#!/usr/bin/env python3
"""
MCP Server — Brazilian Public Job Competitions
- Scraping assíncrono com cache + atualização periódica
- Exposição de ferramentas MCP
- Dois modos de transporte: STDIO (local) e HTTP (cloud)

HTTP: usa FastMCP com ASGI (Uvicorn) e aceita POST /
STDIO: ideal para Cursor/Claude rodando localmente

Comandos:
  # HTTP (Railway/Render/etc.)
  TRANSPORT=http uvicorn mcp_server:http_app --host 0.0.0.0 --port $PORT

  # STDIO (local)
  TRANSPORT=stdio python mcp_server.py
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Any

import httpx
from bs4 import BeautifulSoup

# FastMCP (SDK MCP com suporte HTTP/Streamable HTTP e STDIO)
from fastmcp import FastMCP

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mcp-competitions-br")

# ------------------------------------------------------------------------------
# Estado e cache
# ------------------------------------------------------------------------------
competitions_data: Dict[str, List[Dict[str, str]]] = {}

STATES: Dict[str, str] = {
    "ac": "Acre", "al": "Alagoas", "ap": "Amapá", "am": "Amazonas", "ba": "Bahia",
    "ce": "Ceará", "df": "Distrito Federal", "es": "Espírito Santo", "go": "Goiás",
    "ma": "Maranhão", "mt": "Mato Grosso", "ms": "Mato Grosso do Sul", "mg": "Minas Gerais",
    "pa": "Pará", "pb": "Paraíba", "pr": "Paraná", "pe": "Pernambuco", "pi": "Piauí",
    "rj": "Rio de Janeiro", "rn": "Rio Grande do Norte", "rs": "Rio Grande do Sul",
    "ro": "Rondônia", "rr": "Roraima", "sc": "Santa Catarina", "sp": "São Paulo",
    "se": "Sergipe", "to": "Tocantins"
}

_update_loop_started = False
_first_update_done = False

UPDATE_INTERVAL_SECONDS = int(os.getenv("UPDATE_INTERVAL_SECONDS", "3600"))
SCRAPE_TIMEOUT_SECONDS = float(os.getenv("SCRAPE_TIMEOUT_SECONDS", "30"))
CONCURRENCY = int(os.getenv("SCRAPE_CONCURRENCY", "8"))
USER_AGENT = os.getenv(
    "SCRAPE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ------------------------------------------------------------------------------
# Scraping & Cache
# ------------------------------------------------------------------------------
async def fetch_and_extract_data(state: str, client: httpx.AsyncClient) -> List[Dict[str, str]]:
    """Busca dados mínimos de concursos para um estado."""
    url = f"https://concursosnobrasil.com/concursos/{state}/"
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = await client.get(url, headers=headers, timeout=SCRAPE_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.error("HTTP %s for %s", resp.status_code, state.upper())
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table:
            logger.warning("Table not found for %s", state.upper())
            return []

        out: List[Dict[str, str]] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            org_cell = cells[0]
            link = org_cell.find("a")
            organization = org_cell.get_text(strip=True)
            competition_url = link["href"] if link and "href" in link.attrs else None

            full_text = row.get_text(strip=True).lower()
            if "previsto" in full_text or ("previsto" in organization.lower() if organization else False):
                status = "scheduled"
                organization = organization.replace("previsto", "").strip()
            else:
                status = "open"

            positions = cells[1].get_text(strip=True) if len(cells) > 1 else None

            out.append(
                {
                    "organization": organization,
                    "positions": positions,
                    "status": status,
                    "url": competition_url,
                }
            )
        return out
    except httpx.TimeoutException:
        logger.error("Timeout fetching %s", state.upper())
        return []
    except Exception as e:
        logger.exception("Error processing %s: %s", state.upper(), e)
        return []


async def periodic_update_task():
    """Atualiza todos os estados e popula o cache."""
    logger.info("🔄 Starting competitions update")
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    async with httpx.AsyncClient(limits=limits) as client:
        # limitar concorrência
        sem = asyncio.Semaphore(CONCURRENCY)

        async def _wrapped(s: str):
            async with sem:
                return await fetch_and_extract_data(s, client)

        tasks = [_wrapped(s) for s in STATES.keys()]
        results = await asyncio.gather(*tasks)

    updated = 0
    for s, data in zip(STATES.keys(), results):
        competitions_data[s] = data or []
        if data:
            updated += 1

    logger.info("✅ Update finished: %d/%d states", updated, len(STATES))


async def update_loop():
    """Loop de atualização periódica."""
    global _first_update_done
    # Primeira coleta
    await periodic_update_task()
    _first_update_done = True

    while True:
        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)
        await periodic_update_task()


def process_competitions_data(state: str) -> Dict[str, Any]:
    """Divide em abertas vs previstas."""
    data = competitions_data.get(state, [])
    open_list: List[Dict[str, Any]] = []
    scheduled_list: List[Dict[str, Any]] = []

    for item in data:
        if str(item.get("status", "")).lower() == "scheduled":
            scheduled_list.append(item)
        else:
            open_list.append(item)

    return {
        "state": STATES[state],
        "state_code": state,
        "open_competitions": open_list,
        "scheduled_competitions": scheduled_list,
        "total_open": len(open_list),
        "total_scheduled": len(scheduled_list),
        "message": None if data else "Data is being collected or not available yet. Try again soon.",
    }

# ------------------------------------------------------------------------------
# MCP (FastMCP)
# ------------------------------------------------------------------------------
mcp = FastMCP("competitions-brasil")

# As tools podem ser assíncronas no FastMCP
@mcp.tool()
async def list_all_states() -> str:
    """List all available Brazilian states with their codes (JSON string)."""
    states_list = [{"state_code": code, "name": name} for code, name in STATES.items()]
    return json.dumps(states_list, indent=2, ensure_ascii=False)

@mcp.tool()
async def get_competitions(state: str) -> str:
    """Return competitions for a Brazilian state (JSON string)."""
    global _update_loop_started, _first_update_done
    s = (state or "").lower()
    if s not in STATES:
        return json.dumps({"error": f"Invalid state '{state}'. Use one of: {', '.join(STATES.keys())}"}, ensure_ascii=False)

    # Garante o loop de atualização em background
    if not _update_loop_started:
        _update_loop_started = True
        asyncio.create_task(update_loop())
    # Na primeira chamada, se ainda não populou nada, espera uma coleta rápida
    if not _first_update_done and not competitions_data:
        await periodic_update_task()
        _first_update_done = True

    result = process_competitions_data(s)
    return json.dumps(result, indent=2, ensure_ascii=False)

@mcp.tool()
async def search_competitions_all(filter_open_only: bool = False) -> str:
    """Summarize competitions across all states (optionally only open)."""
    global _update_loop_started, _first_update_done
    if not _update_loop_started:
        _update_loop_started = True
        asyncio.create_task(update_loop())
    if not _first_update_done and not competitions_data:
        await periodic_update_task()
        _first_update_done = True

    out: List[Dict[str, Any]] = []
    for code in STATES.keys():
        result = process_competitions_data(code)
        if filter_open_only:
            out.append(
                {
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "open_competitions": result.get("open_competitions", []),
                    "total_open": result.get("total_open", 0),
                }
            )
        else:
            out.append(
                {
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "total_open": result.get("total_open", 0),
                    "total_scheduled": result.get("total_scheduled", 0),
                }
            )
    return json.dumps(out, indent=2, ensure_ascii=False)

# ------------------------------------------------------------------------------
# HTTP app (FastAPI wrapper com health) + montagem do MCP em "/"
# ------------------------------------------------------------------------------
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, Response

def _build_http_app() -> FastAPI:
    # App MCP ASGI (aceita POST /). O path="/" garante que Cursor poste na raiz.
    mcp_app = mcp.http_app(path="/")

    # Usamos o lifespan do mcp_app no wrapper, para iniciar/fechar corretamente
    api = FastAPI(title="MCP Wrapper", lifespan=mcp_app.lifespan)

    @api.get("/status", include_in_schema=False)
    async def root():
        return JSONResponse({
            "ok": True,
            "service": "mcp-competitions-br",
            "timestamp": int(time.time()),
            "note": "POST / is the MCP endpoint.",
            "http_transport": True
        })

    @api.head("/status", include_in_schema=False)
    async def root_head():
        return PlainTextResponse("", status_code=200)

    @api.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(content=b"", media_type="image/x-icon", status_code=200)

    # Monta o MCP na raiz. As rotas GET/HEAD / acima continuam válidas; POST / cai no MCP.
    api.mount("/", mcp_app)
    return api

# Exposto para Uvicorn: uvicorn mcp_server:http_app
http_app = _build_http_app()

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------
def _env_transport() -> str:
    v = os.getenv("TRANSPORT", "").strip().lower()
    return v if v in {"stdio", "http"} else "stdio"

if __name__ == "__main__":
    transport = _env_transport()
    if transport == "http":
        import uvicorn
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        logger.info("Starting MCP (HTTP) on %s:%s …", host, port)
        uvicorn.run(http_app, host=host, port=port)
    else:
        # STDIO local: faz uma coleta inicial síncrona e inicia o servidor STDIO
        logger.info("Starting MCP (STDIO) …")
        # Populate cache once before starting (opcional)
        try:
            asyncio.run(periodic_update_task())
            _first_update_done = True
            _update_loop_started = True
        except RuntimeError:
            # se já houver loop ativo (ambiente especial), ignora
            pass
        mcp.run(transport="stdio")

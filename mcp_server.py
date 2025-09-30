#!/usr/bin/env python3
"""
MCP Server — Brazilian Public Job Competitions
- Async scraping with cache + periodic refresh
- Exposes MCP tools
- Dual transport: STDIO (local) and HTTP (cloud)

Usage
-----
Local (STDIO, para Cursor/Claude abrirem via subprocess):
    TRANSPORT=stdio python mcp_server.py

Cloud (HTTP, para Railway/Render etc.):
    TRANSPORT=http uvicorn mcp_server:http_app --host 0.0.0.0 --port $PORT

Notas
-----
- O transporte HTTP requer `mcp.server.http` (do pacote `mcp`).
- Em cloud, rode SEMPRE via ASGI server (uvicorn) e `TRANSPORT=http`.
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Any

import httpx
from bs4 import BeautifulSoup

from mcp.server import Server
from mcp.types import Tool, TextContent

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mcp-competitions-br")

# -------------------------------------------------------------------
# Estados e cache
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Scraping & Cache
# -------------------------------------------------------------------
async def fetch_and_extract_data(state: str, client: httpx.AsyncClient) -> List[Dict[str, str]]:
    url = f"https://concursosnobrasil.com/concursos/{state}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = await client.get(url, headers=headers, timeout=30.0)
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
    logger.info("🔄 Starting competitions update")
    async with httpx.AsyncClient() as client:
        tasks = [fetch_and_extract_data(s, client) for s in STATES.keys()]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    updated = 0
    for s, data in zip(STATES.keys(), results):
        competitions_data[s] = data or []
        if data:
            updated += 1

    logger.info("✅ Update finished: %d/%d states", updated, len(STATES))


async def update_loop():
    global _first_update_done
    while True:
        await periodic_update_task()
        _first_update_done = True
        await asyncio.sleep(3600)


def process_competitions_data(state: str) -> Dict[str, Any]:
    data = competitions_data.get(state, [])
    if not data:
        return {
            "state": STATES[state],
            "state_code": state,
            "message": "Data is being collected or not available yet. Try again soon.",
            "open_competitions": [],
            "scheduled_competitions": [],
            "total_open": 0,
            "total_scheduled": 0,
        }

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
    }

# -------------------------------------------------------------------
# MCP Server & Tools
# -------------------------------------------------------------------
mcp_server = Server("competitions-brasil")

@mcp_server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="get_competitions",
            description=(
                "Return competitions for a Brazilian state. "
                f"States: {', '.join(k.upper() for k in STATES.keys())}"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "State code (e.g., sp, rj, mg)",
                        "enum": list(STATES.keys()),
                    }
                },
                "required": ["state"],
            },
        ),
        Tool(
            name="list_all_states",
            description="List all available Brazilian states with their codes.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search_competitions_all",
            description="Summarize competitions across all states (optionally only open).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_open_only": {
                        "type": "boolean",
                        "description": "If true, include only open competitions in each state’s result.",
                        "default": False,
                    }
                },
            },
        ),
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    global _update_loop_started

    # Garante atualização inicial
    if not _update_loop_started:
        _update_loop_started = True
        asyncio.create_task(update_loop())
        if not _first_update_done and not competitions_data:
            logger.info("Running initial data update...")
            await periodic_update_task()

    if name == "list_all_states":
        states_list = [{"state_code": code, "name": name} for code, name in STATES.items()]
        return [TextContent(type="text", text=json.dumps(states_list, indent=2, ensure_ascii=False))]

    if name == "get_competitions":
        state = (arguments or {}).get("state", "").lower()
        if not state or state not in STATES:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Invalid state '{state}'. Use one of: {', '.join(STATES.keys())}"},
                ensure_ascii=False,
            ))]
        result = process_competitions_data(state)
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    if name == "search_competitions_all":
        filter_open_only = bool((arguments or {}).get("filter_open_only", False))
        out: List[Dict[str, Any]] = []
        for code in STATES.keys():
            result = process_competitions_data(code)
            if filter_open_only:
                out.append({
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "open_competitions": result.get("open_competitions", []),
                    "total_open": result.get("total_open", 0),
                })
            else:
                out.append({
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "total_open": result.get("total_open", 0),
                    "total_scheduled": result.get("total_scheduled", 0),
                })
        return [TextContent(type="text", text=json.dumps(out, indent=2, ensure_ascii=False))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

# -------------------------------------------------------------------
# Transport glue (STDIO & HTTP)
# -------------------------------------------------------------------
def _env_transport() -> str:
    v = os.getenv("TRANSPORT", "").strip().lower()
    if v in {"stdio", "http"}:
        return v
    return "stdio"

async def _run_stdio():
    import mcp.server.stdio
    logger.info("Starting MCP (STDIO) …")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await mcp_server.run(read_stream, write_stream, mcp_server.create_initialization_options())

# ---------- HTTP: cria ASGI + health wrapper ----------
# Importa o transporte HTTP e constrói o app no import (para uvicorn "module:var")
_HTTP_AVAILABLE = False
try:
    import mcp.server.http  # precisa existir nessa versão do pacote 'mcp'
    _HTTP_AVAILABLE = True
except Exception as e:
    logger.warning("mcp.server.http not available: %s", e)

def _build_http_app_or_fallback():
    """
    Monta o app ASGI do MCP e adiciona health handlers (GET/HEAD / e favicon).
    Se o transporte HTTP não existir, expõe fallback 503 (mas nunca retorna None).
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse, Response

    if _HTTP_AVAILABLE:
        try:
            mcp_app = mcp.server.http.app_from_server(mcp_server)
        except Exception as e:
            logger.exception("Failed to build MCP HTTP app: %s", e)
            mcp_app = None
    else:
        mcp_app = None

    api = FastAPI(title="MCP Wrapper")

    @api.get("/", include_in_schema=False)
    async def root():
        return JSONResponse({
            "ok": True,
            "service": "mcp-competitions-br",
            "timestamp": int(time.time()),
            "note": "POST / is the MCP endpoint.",
            "http_transport": bool(mcp_app is not None),
        })

    @api.head("/", include_in_schema=False)
    async def root_head():
        return PlainTextResponse("", status_code=200)

    @api.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        # evita 404 em health de navegadores / edge
        return Response(content=b"", media_type="image/x-icon", status_code=200)

    # Se o MCP HTTP estiver disponível, montamos na raiz para capturar POST /
    if mcp_app is not None:
        # Nossas rotas GET/HEAD/GET-favicon continuam válidas; POST / cai no MCP.
        api.mount("/", mcp_app)

    return api

# Exposto para o uvicorn: uvicorn mcp_server:http_app
http_app = _build_http_app_or_fallback()

# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    transport = _env_transport()
    if transport == "http":
        import uvicorn
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        logger.info("Starting MCP (HTTP) on %s:%s …", host, port)
        uvicorn.run(http_app, host=host, port=port)
    else:
        asyncio.run(_run_stdio())

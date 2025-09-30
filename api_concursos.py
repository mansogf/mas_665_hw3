#!/usr/bin/env python3
"""
Brazilian Public Job Competitions API - FastAPI
- Coleta assíncrona com cache + atualização periódica (APScheduler)
- Filtros por estado/status/busca
- Health e favicon para cloud hosting (Railway/Render/etc.)
"""

import asyncio
import os
import time
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import logging
from dataclasses import dataclass, asdict
from enum import Enum

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import httpx
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ------------------------------------------------------------------------------
# Config (ENV)
# ------------------------------------------------------------------------------
UPDATE_INTERVAL_SECONDS = int(os.getenv("UPDATE_INTERVAL_SECONDS", "3600"))
SCRAPE_TIMEOUT_SECONDS  = float(os.getenv("SCRAPE_TIMEOUT_SECONDS",  "30"))
SCRAPE_CONCURRENCY      = int(os.getenv("SCRAPE_CONCURRENCY",       "8"))
USER_AGENT = os.getenv(
    "SCRAPE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("api_concursos")

# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = FastAPI(
    title="Brazilian Public Job Competitions API",
    description="Complete API for querying Brazilian public job competitions",
    version="2.2.0",
)

# Middlewares
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Tipos / modelos
# ------------------------------------------------------------------------------
class CompetitionStatus(str, Enum):
    OPEN = "open"
    SCHEDULED = "scheduled"

@dataclass
class Competition:
    organization: str
    positions: Optional[str]
    status: CompetitionStatus
    url: Optional[str]

@dataclass
class CacheMetadata:
    last_update: str
    next_update: str
    total_competitions: int
    success: bool
    update_time_seconds: float

# ------------------------------------------------------------------------------
# Cache
# ------------------------------------------------------------------------------
competitions_data: Dict[str, List[Competition]] = {}
cache_metadata: Dict[str, CacheMetadata] = {}
global_statistics = {
    "total_updates": 0,
    "total_errors": 0,
    "last_update_time_seconds": 0.0,
    "last_complete_update": None,
}

STATES: Dict[str, str] = {
    "ac": "Acre", "al": "Alagoas", "ap": "Amapá", "am": "Amazonas",
    "ba": "Bahia", "ce": "Ceará", "df": "Distrito Federal",
    "es": "Espírito Santo", "go": "Goiás", "ma": "Maranhão",
    "mt": "Mato Grosso", "ms": "Mato Grosso do Sul", "mg": "Minas Gerais",
    "pa": "Pará", "pb": "Paraíba", "pr": "Paraná", "pe": "Pernambuco",
    "pi": "Piauí", "rj": "Rio de Janeiro", "rn": "Rio Grande do Norte",
    "rs": "Rio Grande do Sul", "ro": "Rondônia", "rr": "Roraima",
    "sc": "Santa Catarina", "sp": "São Paulo", "se": "Sergipe",
    "to": "Tocantins",
}

# ------------------------------------------------------------------------------
# Coleta
# ------------------------------------------------------------------------------
async def fetch_and_extract_data(state: str, client: httpx.AsyncClient) -> tuple[List[Competition], bool]:
    url = f"https://concursosnobrasil.com/concursos/{state}/"
    headers = {"User-Agent": USER_AGENT}
    try:
        start = time.perf_counter()
        r = await client.get(url, headers=headers, timeout=SCRAPE_TIMEOUT_SECONDS)
        if r.status_code != 200:
            logger.error("HTTP %s for %s", r.status_code, state.upper())
            return [], False

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            logger.warning("Table not found: %s", state.upper())
            return [], False

        competitions: List[Competition] = []
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
                status = CompetitionStatus.SCHEDULED
                organization = organization.replace("previsto", "").strip()
            else:
                status = CompetitionStatus.OPEN

            positions = cells[1].get_text(strip=True) if len(cells) > 1 else None
            competitions.append(Competition(organization=organization, positions=positions, status=status, url=competition_url))

        elapsed = time.perf_counter() - start
        logger.info("%s: %d competitions in %.2fs", state.upper(), len(competitions), elapsed)
        return competitions, True

    except httpx.TimeoutException:
        logger.error("Timeout: %s", state.upper())
        global_statistics["total_errors"] += 1
        return [], False
    except Exception as e:
        logger.exception("Error processing %s: %s", state.upper(), e)
        global_statistics["total_errors"] += 1
        return [], False

async def _gather_with_semaphore(coros: List[asyncio.Task], limit: int):
    sem = asyncio.Semaphore(limit)
    async def _wrap(coro):
        async with sem:
            return await coro
    return await asyncio.gather(*[_wrap(c) for c in coros])

async def periodic_update_task():
    logger.info("🔄 Starting periodic update")
    global_start = time.perf_counter()

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=40)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [fetch_and_extract_data(s, client) for s in STATES.keys()]
        results = await _gather_with_semaphore(tasks, SCRAPE_CONCURRENCY)

        successes = 0
        now = datetime.now()
        for state, (comps, ok) in zip(STATES.keys(), results):
            if ok and comps:
                competitions_data[state] = comps
                successes += 1
                cache_metadata[state] = CacheMetadata(
                    last_update=now.isoformat(),
                    next_update=(now + timedelta(seconds=UPDATE_INTERVAL_SECONDS)).isoformat(),
                    total_competitions=len(comps),
                    success=True,
                    update_time_seconds=time.perf_counter() - global_start,
                )
            else:
                competitions_data.setdefault(state, [])
                cache_metadata[state] = CacheMetadata(
                    last_update=now.isoformat(),
                    next_update=(now + timedelta(seconds=UPDATE_INTERVAL_SECONDS)).isoformat(),
                    total_competitions=0,
                    success=False,
                    update_time_seconds=0.0,
                )

    total_time = time.perf_counter() - global_start
    global_statistics["total_updates"] += 1
    global_statistics["last_update_time_seconds"] = total_time
    global_statistics["last_complete_update"] = datetime.now().isoformat()
    logger.info("✅ Update completed: %d/%d states in %.2fs", successes, len(STATES), total_time)

def process_competitions_data(
    state: str,
    status_filter: Optional[CompetitionStatus] = None,
    search: Optional[str] = None
) -> Dict[str, Any]:
    comps = competitions_data.get(state, [])
    metadata = cache_metadata.get(state)

    if not comps:
        return {
            "state": STATES[state],
            "state_code": state,
            "message": "Data being collected or not available",
            "open_competitions": [],
            "scheduled_competitions": [],
            "metadata": asdict(metadata) if metadata else None,
        }

    filtered = comps
    if status_filter:
        filtered = [c for c in filtered if c.status == status_filter]
    if search:
        s = search.lower()
        filtered = [c for c in filtered if s in c.organization.lower()]

    open_comp  = [asdict(c) for c in filtered if c.status == CompetitionStatus.OPEN]
    sched_comp = [asdict(c) for c in filtered if c.status == CompetitionStatus.SCHEDULED]

    return {
        "state": STATES[state],
        "state_code": state,
        "open_competitions": open_comp,
        "scheduled_competitions": sched_comp,
        "total_open": len(open_comp),
        "total_scheduled": len(sched_comp),
        "metadata": asdict(metadata) if metadata else None,
    }

# ------------------------------------------------------------------------------
# Startup / Shutdown
# ------------------------------------------------------------------------------
_scheduler: Optional[AsyncIOScheduler] = None

@app.on_event("startup")
async def startup_event():
    global _scheduler
    # Atualização inicial
    await periodic_update_task()

    # Agenda atualizações
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(periodic_update_task, "interval", seconds=UPDATE_INTERVAL_SECONDS, id="refresh")
    _scheduler.start()
    logger.info("🚀 API started | interval=%ss concurrency=%s timeout=%ss",
                UPDATE_INTERVAL_SECONDS, SCRAPE_CONCURRENCY, SCRAPE_TIMEOUT_SECONDS)

@app.on_event("shutdown")
async def shutdown_event():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    logger.info("🟡 API shutdown")

# ------------------------------------------------------------------------------
# Rotas
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    state_list_html = "".join([f'<li><a href="/states/{s}">{s.upper()}</a> - {name}</li>' for s, name in STATES.items()])
    html = f"""
    <!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Brazil Competitions API</title>
    <style>body{{font-family:system-ui;max-width:1100px;margin:0 auto;padding:2rem}}h1{{color:#2563eb}}
    .endpoint{{background:#f3f4f6;padding:1rem;margin:1rem 0;border-radius:8px}}code{{background:#e5e7eb;padding:.2rem .5rem;border-radius:4px}}
    .state-list{{column-count:4;list-style:none;padding:0}}.feature{{background:#ecfccb;padding:1rem;margin:.5rem 0;border-radius:8px}}
    .muted{{color:#6b7280;font-size:.9rem}}</style>
    </head><body>
    <h1>🎯 Brazilian Public Job Competitions API v2.2</h1>
    <p class="muted">Update interval: {UPDATE_INTERVAL_SECONDS}s · Concurrency: {SCRAPE_CONCURRENCY} · Timeout: {SCRAPE_TIMEOUT_SECONDS}s</p>
    <div class="feature"><strong>✨ Features:</strong> Coleta assíncrona, cache por estado, filtros e métricas</div>
    <h2>📍 Endpoints</h2>
    <div class="endpoint"><code>GET /states/{{state}}</code> — por estado<br>
    params: <code>status</code>=open|scheduled, <code>search</code>=texto</div>
    <div class="endpoint"><code>GET /stats/global</code> — estatísticas gerais</div>
    <div class="endpoint"><code>GET /health</code> — health check</div>
    <h2>🗺️ States</h2><ul class="state-list">{state_list_html}</ul>
    <h2>📖 Examples</h2>
    <div class="endpoint"><code>/states/sp?status=open</code><br><code>/states/rj?search=municipal</code></div>
    </body></html>
    """
    return HTMLResponse(content=html)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Evita 404 em navegadores e no edge
    return Response(content=b"", media_type="image/x-icon", status_code=200)

@app.get("/stats/global")
async def get_stats():
    total_competitions = sum(len(c) for c in competitions_data.values())
    updated_states = len([m for m in cache_metadata.values() if m.success])
    return {
        "total_competitions": total_competitions,
        "available_states": len(STATES),
        "updated_states": updated_states,
        "statistics": global_statistics,
        "by_state": {
            s: {
                "total": len(competitions_data.get(s, [])),
                "open": len([c for c in competitions_data.get(s, []) if c.status == CompetitionStatus.OPEN]),
                "scheduled": len([c for c in competitions_data.get(s, []) if c.status == CompetitionStatus.SCHEDULED]),
            }
            for s in STATES.keys()
        },
    }

@app.get("/health")
async def health_check():
    healthy_states = sum(1 for m in cache_metadata.values() if m.success)
    percentage = (healthy_states / len(STATES)) * 100
    status = "healthy" if percentage >= 80 else "degraded" if percentage >= 50 else "unhealthy"
    return {
        "status": status,
        "available_states": healthy_states,
        "total_states": len(STATES),
        "availability_percentage": f"{percentage:.1f}%",
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/states/{state_code}")
async def get_competitions_endpoint(
    state_code: str,
    status: Optional[CompetitionStatus] = Query(None, description="Filter by status"),
    search: Optional[str] = Query(None, description="Search by text"),
):
    state_lower = state_code.lower()
    if state_lower not in STATES:
        raise HTTPException(status_code=404, detail=f"State '{state_code}' not found")
    return process_competitions_data(state_lower, status, search)

# Dev only
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))

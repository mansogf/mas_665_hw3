"""
Brazilian Public Job Competitions API - Enhanced FastAPI
Version with complete data extraction, intelligent cache and metrics
"""

import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import logging
from dataclasses import dataclass, asdict
from enum import Enum

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("api_concursos")

app = FastAPI(
    title="Brazilian Public Job Competitions API",
    description="Complete API for querying Brazilian public job competitions",
    version="2.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enums / modelos
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

# Cache
competitions_data: Dict[str, List[Competition]] = {}
cache_metadata: Dict[str, CacheMetadata] = {}
global_statistics = {
    "total_updates": 0,
    "total_errors": 0,
    "last_update_time_seconds": 0.0,
    "last_complete_update": None,
}

# Estados
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

# ===== coleta =====
async def fetch_and_extract_data(state: str, client: httpx.AsyncClient) -> tuple[List[Competition], bool]:
    url = f"https://concursosnobrasil.com/concursos/{state}/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        start = datetime.now()
        r = await client.get(url, headers=headers, timeout=30.0)
        if r.status_code != 200:
            logger.error(f"HTTP {r.status_code} for {state.upper()}")
            return [], False

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            logger.warning(f"Table not found: {state.upper()}")
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
            if "previsto" in full_text or "previsto" in organization.lower():
                status = CompetitionStatus.SCHEDULED
                organization = organization.replace("previsto", "").strip()
            else:
                status = CompetitionStatus.OPEN

            positions = cells[1].get_text(strip=True) if len(cells) > 1 else None
            competitions.append(Competition(organization=organization, positions=positions, status=status, url=competition_url))

        elapsed = (datetime.now() - start).total_seconds()
        logger.info(f"{state.upper()}: {len(competitions)} competitions extracted in {elapsed:.2f}s")
        return competitions, True

    except httpx.TimeoutException:
        logger.error(f"Timeout: {state.upper()}")
        global_statistics["total_errors"] += 1
        return [], False
    except Exception as e:
        logger.error(f"Error processing {state.upper()}: {e}")
        global_statistics["total_errors"] += 1
        return [], False

async def periodic_update_task():
    logger.info("🔄 Starting periodic update")
    global_start = datetime.now()
    async with httpx.AsyncClient() as client:
        tasks = [fetch_and_extract_data(s, client) for s in STATES.keys()]
        results = await asyncio.gather(*tasks)

        successes = 0
        for state, (comps, ok) in zip(STATES.keys(), results):
            if ok and comps:
                competitions_data[state] = comps
                successes += 1
                cache_metadata[state] = CacheMetadata(
                    last_update=datetime.now().isoformat(),
                    next_update=(datetime.now() + timedelta(hours=1)).isoformat(),
                    total_competitions=len(comps),
                    success=True,
                    update_time_seconds=(datetime.now() - global_start).total_seconds(),
                )
            else:
                competitions_data.setdefault(state, [])
                cache_metadata[state] = CacheMetadata(
                    last_update=datetime.now().isoformat(),
                    next_update=(datetime.now() + timedelta(hours=1)).isoformat(),
                    total_competitions=0,
                    success=False,
                    update_time_seconds=0.0,
                )

    total_time = (datetime.now() - global_start).total_seconds()
    global_statistics["total_updates"] += 1
    global_statistics["last_update_time_seconds"] = total_time
    global_statistics["last_complete_update"] = datetime.now().isoformat()
    logger.info(f"✅ Update completed: {successes}/{len(STATES)} states in {total_time:.2f}s")

def process_competitions_data(state: str, status_filter: Optional[CompetitionStatus] = None, search: Optional[str] = None) -> Dict[str, Any]:
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

    open_comp = [asdict(c) for c in filtered if c.status == CompetitionStatus.OPEN]
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

# ===== startup =====
@app.on_event("startup")
async def startup_event():
    await periodic_update_task()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_update_task, "interval", hours=1)
    scheduler.start()
    logger.info("🚀 Server started successfully")

# ===== rotas estáticas =====
@app.get("/", response_class=HTMLResponse)
async def root():
    state_list_html = "".join([f'<li><a href="/states/{s}">{s.upper()}</a> - {name}</li>' for s, name in STATES.items()])
    html = f"""
    <!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Brazil Competitions API</title>
    <style>body{{font-family:system-ui;max-width:1200px;margin:0 auto;padding:2rem}}h1{{color:#2563eb}}
    .endpoint{{background:#f3f4f6;padding:1rem;margin:1rem 0;border-radius:8px}}code{{background:#e5e7eb;padding:.2rem .5rem;border-radius:4px}}
    .state-list{{column-count:4;list-style:none;padding:0}}.feature{{background:#ecfccb;padding:1rem;margin:.5rem 0;border-radius:8px}}</style>
    </head><body>
    <h1>🎯 Brazilian Public Job Competitions API v2.0</h1>
    <div class="feature"><strong>✨ Features:</strong> Essential data, filters, cache, metrics</div>
    <h2>📍 Endpoints</h2>
    <div class="endpoint"><code>GET /states/{{state}}</code> - por estado<br>
    params: <code>status</code>=open|scheduled, <code>search</code>=texto</div>
    <div class="endpoint"><code>GET /stats/global</code> - estatísticas</div>
    <div class="endpoint"><code>GET /health</code> - saúde do sistema</div>
    <h2>🗺️ Estados</h2><ul class="state-list">{state_list_html}</ul>
    <h2>📖 Exemplos</h2>
    <div class="endpoint"><code>/states/sp?status=open</code><br><code>/states/rj?search=prefeitura</code></div>
    </body></html>
    """
    return HTMLResponse(content=html)

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

# ===== rota dinâmica com prefixo =====
@app.get("/states/{state_code}")
async def get_competitions(
    state_code: str,
    status: Optional[CompetitionStatus] = Query(None, description="Filter by status"),
    search: Optional[str] = Query(None, description="Search by text"),
):
    state_lower = state_code.lower()
    if state_lower not in STATES:
        raise HTTPException(status_code=404, detail=f"State '{state_code}' not found")
    return process_competitions_data(state_lower, status, search)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

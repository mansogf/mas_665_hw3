## Brazilian Public Job Competitions - API and MCP Server

This repository contains two main services:
1. **FastAPI Service** (`api_concursos.py`) - Main web API with advanced features like filtering, stats, and health monitoring
2. **MCP Server** (`mcp_server.py`) - Model Context Protocol server for IDE/agent integration (like Cursor)

### Requirements
- Python 3.10+
- pip

Install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

### 1. FastAPI Service (`api_concursos.py`)

**Main web API** with advanced features like filtering, stats, and health monitoring. Periodically scrapes `concursosnobrasil.com` for each Brazilian state and caches essential fields per competition:
- organization
- positions (text as shown on the site)
- status: `open` or `scheduled`
- url

It updates all states on startup and every hour thereafter.

**Run:**
```bash
python api_concursos.py
# or
uvicorn api_concursos:app --host 0.0.0.0 --port 8000
```
**URL:** `http://localhost:8000`

**Endpoints:**
- `GET /` — Home page with quick docs and links for each state
- `GET /states/{state_code}` — Competitions for a state
  - Query params: `status` (`open` | `scheduled`), `search` (free-text match)
- `GET /stats/global` — Global statistics and per-state totals
- `GET /health` — Health summary based on availability across states

**Test Examples:**
```bash
# Open competitions in São Paulo
curl 'http://localhost:8000/states/sp?status=open'

# Search for "prefeitura" in Rio de Janeiro
curl 'http://localhost:8000/states/rj?search=prefeitura'

# Health and stats
curl 'http://localhost:8000/health'
curl 'http://localhost:8000/stats/global'
```

### 2. MCP Server (`mcp_server.py`)

**Model Context Protocol server** for IDE/agent integration (like Cursor). Exposes tools via MCP so compatible clients can query competitions directly.

**Run:**
```bash
python mcp_server.py
```

On first call, the server performs an initial data update and then refreshes hourly.

**Tools:**
- `list_all_states()` → Returns available states
- `get_competitions({ state })` → Returns competitions for the given state code
- `search_competitions_all({ filter_open_only })` → Aggregates results across all states

**Test via Cursor:**
1. Configure MCP in `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "competitions-brasil": {
      "command": "/usr/bin/python3",
      "args": ["/Users/gmanso/MIT Dropbox/Gabriel Manso/MIT/github/mas_665/hw3/mcp_server.py"]
    }
  }
}
```
2. Restart Cursor
3. Use the tools in chat: "List all Brazilian states" or "Get competitions from São Paulo"

**Available state codes:** `ac, al, ap, am, ba, ce, df, es, go, ma, mt, ms, mg, pa, pb, pr, pe, pi, rj, rn, rs, ro, rr, sc, sp, se, to`

**Data Structure:**
Each competition includes:
- `organization`: The organization hosting the competition
- `positions`: Available positions/roles (as text from the site)
- `status`: Either `"open"` or `"scheduled"`
- `url`: Direct link to the competition details

## Quick Testing Guide

### Test Both Services

1. **Terminal 1** - Main FastAPI Service:
```bash
python api_concursos.py
# Test: curl 'http://localhost:8000/health'
```

2. **Terminal 2** - MCP Server:
```bash
python mcp_server.py
# Test: Configure in Cursor and use tools in chat
```

### Automated Testing Script
Create `test_services.py`:
```python
import asyncio
import httpx
import json

async def test_services():
    services = [
        ("Main API", "http://localhost:8000")
    ]
    
    async with httpx.AsyncClient() as client:
        for name, url in services:
            try:
                # Test health
                health = await client.get(f"{url}/health")
                print(f"{name}: {health.status_code} - {health.json()}")
                
                # Test state endpoint
                states = await client.get(f"{url}/states/sp")
                print(f"{name} SP data: {len(states.json().get('open_competitions', []))} open competitions")
                
            except Exception as e:
                print(f"{name}: ERROR - {e}")

if __name__ == "__main__":
    asyncio.run(test_services())
```

Run with: `python test_services.py`

### Notes on Data Collection
- **Source:** `https://concursosnobrasil.com/concursos/{state_code}/`
- **Update Frequency:** Every hour (automatic)
- **User-Agent header** used to improve reliability
- **Table structure** varies; first two columns are typically organization and positions
- **Status detection:** "previsto" → `scheduled`, otherwise → `open`
- **Data extraction:** Organization, positions, status, and URL for each competition

### Troubleshooting
- **Missing imports**: Ensure virtual environment is active and `pip install -r requirements.txt` completed
- **HTTP errors/timeouts**: Upstream site may be rate-limiting; try again later
- **Empty results**: Data might still be loading; updater runs on startup and hourly
- **MCP not working**: Check `~/.cursor/mcp.json` path and restart Cursor

### License
Educational use for coursework; verify terms before production use.



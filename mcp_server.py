#!/usr/bin/env python3
"""
MCP Server - Brazilian Public Job Competitions
Python version with cache and periodic updates
"""

import asyncio
import json
import logging
from typing import Dict, List, Any
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Competition data cache
competitions_data: Dict[str, List[Dict[str, str]]] = {}

# Brazilian States List
STATES: Dict[str, str] = {
    "ac": "Acre", "al": "Alagoas", "ap": "Amapá", "am": "Amazonas", "ba": "Bahia",
    "ce": "Ceará", "df": "Distrito Federal", "es": "Espírito Santo", "go": "Goiás",
    "ma": "Maranhão", "mt": "Mato Grosso", "ms": "Mato Grosso do Sul", "mg": "Minas Gerais",
    "pa": "Pará", "pb": "Paraíba", "pr": "Paraná", "pe": "Pernambuco", "pi": "Piauí",
    "rj": "Rio de Janeiro", "rn": "Rio Grande do Norte", "rs": "Rio Grande do Sul",
    "ro": "Rondônia", "rr": "Roraima", "sc": "Santa Catarina", "sp": "São Paulo",
    "se": "Sergipe", "to": "Tocantins"
}

# Initialization flags
update_task_started = False
first_update_completed = False


async def fetch_and_extract_data(state: str, client: httpx.AsyncClient) -> List[Dict[str, str]]:
    """Fetches and extracts essential data (organization, positions, status, url) for a specific state."""
    url = f"https://concursosnobrasil.com/concursos/{state}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = await client.get(url, headers=headers, timeout=30.0)
        
        if response.status_code != 200:
            logger.error(f"HTTP error when searching {state.upper()}: {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table")

        if not table:
            logger.warning(f"No table found for {state.upper()}")
            return []

        results: List[Dict[str, str]] = []

        # Headers may vary; however, the first two columns are usually organization and positions
        for row in table.find_all("tr")[1:]:  # Skip header
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            org_cell = cells[0]
            link = org_cell.find("a")
            organization = org_cell.get_text(strip=True)
            competition_url = link["href"] if link and "href" in link.attrs else None

            # Determine status from row text
            full_text = row.get_text(strip=True).lower()
            if "previsto" in full_text or (organization and "previsto" in organization.lower()):
                status = "scheduled"
                organization = organization.replace("previsto", "").strip()
            else:
                status = "open"

            positions = cells[1].get_text(strip=True) if len(cells) > 1 else None

            results.append({
                "organization": organization,
                "positions": positions,
                "status": status,
                "url": competition_url,
            })

        return results

    except httpx.TimeoutException:
        logger.error(f"Timeout when fetching data for {state.upper()}")
        return []
    except Exception as e:
        logger.error(f"Error processing {state.upper()}: {e}")
        return []


async def periodic_update_task():
    """Periodic task that updates data for all states."""
    logger.info("Starting competition data update cycle...")
    
    async with httpx.AsyncClient() as client:
        tasks = [fetch_and_extract_data(state, client) for state in STATES.keys()]
        results = await asyncio.gather(*tasks)

        for state, data in zip(STATES.keys(), results):
            if data and len(data) > 0:
                competitions_data[state] = data
                logger.info(f"{state.upper()}: {len(data)} records updated")
            else:
                if state not in competitions_data:
                    competitions_data[state] = []
                logger.warning(f"Could not get new data for {state.upper()}")

    logger.info("Update cycle completed. Next update in 1 hour.")


async def update_loop():
    """Loop that executes periodic updates."""
    global first_update_completed
    while True:
        await periodic_update_task()
        first_update_completed = True
        await asyncio.sleep(3600)  # 1 hour


def process_competitions_data(state: str) -> Dict[str, Any]:
    """Processes and separates competition data for a state, already in the essential new schema."""
    data = competitions_data.get(state, [])

    if not data:
        return {
            "state": STATES[state],
            "state_code": state,
            "message": "Data being collected or no competitions available. Try again in a few seconds.",
            "open_competitions": [],
            "scheduled_competitions": []
        }

    open_competitions: List[Dict[str, Any]] = []
    scheduled_competitions: List[Dict[str, Any]] = []

    for competition in data:
        status_value = str(competition.get("status", "")).lower()
        if status_value == "scheduled":
            scheduled_competitions.append(competition)
        else:
            open_competitions.append(competition)

    return {
        "state": STATES[state],
        "state_code": state,
        "open_competitions": open_competitions,
        "scheduled_competitions": scheduled_competitions,
        "total_open": len(open_competitions),
        "total_scheduled": len(scheduled_competitions)
    }


# Create MCP server
app = Server("competitions-brasil")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Lists available tools."""
    return [
        Tool(
            name="get_competitions",
            description=f"Searches public job competitions from a Brazilian state. Available states: {', '.join(k.upper() for k in STATES.keys())}",
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "State code (ex: sp, rj, mg)",
                        "enum": list(STATES.keys()),
                    }
                },
                "required": ["state"],
            }
        ),
        Tool(
            name="list_all_states",
            description="Lists all available Brazilian states with their codes",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="search_competitions_all",
            description="Searches competitions in all Brazilian states (may take time)",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_open_only": {
                        "type": "boolean",
                        "description": "If true, returns only open competitions",
                        "default": False,
                    }
                }
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Executes tools."""
    global update_task_started
    
    # Ensures the update loop is running and performs an initial update if necessary
    if not update_task_started:
        update_task_started = True
        asyncio.create_task(update_loop())
        if not first_update_completed and not competitions_data:
            logger.info("Executing initial data update...")
            await periodic_update_task()

    if name == "list_all_states":
        states_list = [
            {"state_code": state, "name": name}
            for state, name in STATES.items()
        ]
        return [
            TextContent(
                type="text",
                text=json.dumps(states_list, indent=2, ensure_ascii=False)
            )
        ]

    elif name == "get_competitions":
        state = arguments.get("state", "").lower()

        if not state or state not in STATES:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"State '{arguments.get('state')}' invalid. Use: {', '.join(STATES.keys())}"
                    }, ensure_ascii=False)
                )
            ]

        result = process_competitions_data(state)
        return [
            TextContent(
                type="text",
                text=json.dumps(result, indent=2, ensure_ascii=False)
            )
        ]

    elif name == "search_competitions_all":
        filter_open_only = arguments.get("filter_open_only", False)
        all_results = []

        for state in STATES.keys():
            result = process_competitions_data(state)

            if filter_open_only:
                all_results.append({
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "open_competitions": result.get("open_competitions", []),
                    "total_open": result.get("total_open", 0),
                })
            else:
                all_results.append({
                    "state": result["state"],
                    "state_code": result["state_code"],
                    "total_open": result.get("total_open", 0),
                    "total_scheduled": result.get("total_scheduled", 0),
                })

        return [
            TextContent(
                type="text",
                text=json.dumps(all_results, indent=2, ensure_ascii=False)
            )
        ]

    return [
        TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"})
        )
    ]


async def main():
    """Main function that starts the MCP server."""
    logger.info("MCP Server Competitions Brasil starting...")
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
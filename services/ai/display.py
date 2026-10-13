"""
Display tools: how the agent puts tables in front of the user
(docs/AI_AGENT_PLAN.md § 2-3).

The model never types a table. It calls `show_table` naming a tool result it
already has ("r3"), which list in it to show, and which columns; the server
copies the values out of the stored result. So every number in a table is a
number a lookup returned, by construction, and a column the result doesn't
have is an error the model can read and fix -- never a made-up value.

A table can join several results of the same shape (one game log per player):
their rows are concatenated, and each row can show its parent result's
top-level fields (the player's `name` beside each of his games).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_COLUMNS = 10
MAX_ROWS = 40
_RESULT_ID = re.compile(r"^r[1-9][0-9]{0,3}$")

SHOW_TABLE: dict[str, Any] = {
    "name": "show_table",
    "description": (
        "Show the user a table built from tool results you already have. Name the result(s) by "
        "their result_id, the list inside them to show (`rows`, a key such as 'players' or "
        "'your_roster'; a dotted path for a nested list), and the columns (keys of that list's "
        "items, or of the result itself, such as a player's `name` beside each of his games). "
        "Several result_ids of the same kind are joined into one table. The server copies the "
        "values: use this instead of writing numbers out in a list or a markdown table."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ids": {"type": "array", "items": {"type": "string"},
                           "description": "One or more result_ids, e.g. ['r3'] or ['r4', 'r5', 'r6']"},
            "rows": {"type": "string", "description": "Key of the list to show; may be omitted when "
                                                      "the result holds exactly one list"},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": f"Keys to show, in order, at most {MAX_COLUMNS}"},
            "sort_by": {"type": "string", "description": "A column to sort by"},
            "descending": {"type": "boolean", "description": "Sort high to low (default true)"},
            "limit": {"type": "integer", "description": f"Most rows to show, 1-{MAX_ROWS} (default 20)"},
            "title": {"type": "string", "description": "A short title, e.g. 'Free agents: blocks, last 5 games'"},
        },
        "required": ["result_ids", "columns", "title"],
        "additionalProperties": False,
    },
}
DISPLAY_TOOL_NAMES = frozenset({SHOW_TABLE["name"]})

# Column headers for the keys the read tools return; anything else is shown as its key
LABELS: dict[str, str] = {
    "name": "Player", "team": "Team", "position": "Pos", "positions": "Pos", "slot": "Slot",
    "avg_value": "Avg", "avg_value_last_7": "Avg L7", "avg_value_season": "Avg season",
    "games_left": "Games left", "game_days": "Game days", "back_to_backs": "B2B",
    "stream_score": "Stream score", "injury": "Injury", "acquisition": "Pickup",
    "waivers_until": "Waivers until", "date": "Date", "opponent": "Opp", "home": "Home",
    "min": "MIN", "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK", "tov": "TOV",
    "fgm": "FGM", "fga": "FGA", "fg3m": "3PM", "fg3a": "3PA", "ftm": "FTM", "fta": "FTA",
    "fpts": "CV FPTS", "category": "Cat", "label": "Category", "you": "You", "leader": "Leader",
    "projected_you": "Proj you", "projected_opponent": "Proj opp", "projected_leader": "Proj leader",
    "games_played": "GP", "avg_fpts": "Avg FPTS", "rank": "Rank", "status": "Status",
}


class _ShowTableInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_ids: list[str] = Field(..., min_length=1, max_length=12)
    rows: Optional[str] = Field(None, max_length=60)
    columns: list[str] = Field(..., min_length=1, max_length=MAX_COLUMNS)
    sort_by: Optional[str] = Field(None, max_length=60)
    descending: bool = True
    limit: int = Field(20, ge=1, le=MAX_ROWS)
    title: str = Field(..., min_length=1, max_length=100)


class DisplayError(Exception):
    """A display call the server refused; the message goes back to the model."""


@dataclass(frozen=True)
class Shown:
    block: dict[str, Any]
    summary: str


def _lists_in(data: dict[str, Any]) -> list[str]:
    return [k for k, v in data.items() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]


def _rows_of(data: dict[str, Any], path: Optional[str], result_id: str) -> list[dict[str, Any]]:
    if path is None:
        lists = _lists_in(data)
        if len(lists) != 1:
            raise DisplayError(f"{result_id} has {len(lists) or 'no'} lists of rows "
                               f"({', '.join(lists) or 'none'}); name one in `rows`")
        path = lists[0]
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise DisplayError(f"{result_id} has no '{path}'")
        node = node[key]
    if not isinstance(node, list) or not all(isinstance(i, dict) for i in node):
        raise DisplayError(f"'{path}' in {result_id} is not a list of rows")
    # Each row may show its parent's own fields (a player's name beside his games)
    parent = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
    return [{**parent, **row} for row in node]


def _cell(value: Any, column: str) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list) and all(not isinstance(v, (list, dict)) for v in value):
        return ", ".join(str(v) for v in value)
    raise DisplayError(f"'{column}' is not a simple column")


def _sort_key(value: Any) -> tuple[int, Any]:
    # None sorts last whichever way the table is sorted (see the reverse below)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return (1, str(value) if value is not None else "")
    return (0, value)


def show_table(raw_input: Any, results: dict[str, Any], next_id: str) -> Shown:
    """Build one table block from stored results, or raise DisplayError."""
    try:
        args = _ShowTableInput.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        raise DisplayError(f"Invalid input: {problems}") from exc

    rows: list[dict[str, Any]] = []
    tools: list[str] = []
    for rid in args.result_ids:
        stored = results.get(rid) if _RESULT_ID.match(rid) else None
        if stored is None:
            raise DisplayError(f"No result {rid}: use a result_id a lookup returned")
        rows.extend(_rows_of(stored["data"], args.rows, rid))
        tools.append(stored["tool"])
    if not rows:
        raise DisplayError("Those results have no rows to show")

    known = set().union(*(row.keys() for row in rows))
    missing = [c for c in [*args.columns, *([args.sort_by] if args.sort_by else [])] if c not in known]
    if missing:
        raise DisplayError(f"No column {', '.join(repr(c) for c in missing)}; the rows have: {', '.join(sorted(known))}")

    if args.sort_by:
        present = [r for r in rows if r.get(args.sort_by) is not None]
        absent = [r for r in rows if r.get(args.sort_by) is None]
        rows = sorted(present, key=lambda r: _sort_key(r[args.sort_by]), reverse=args.descending) + absent
    shown = rows[: args.limit]
    block = {
        "type": "table",
        "id": next_id,
        "title": args.title,
        "result_ids": args.result_ids,
        "tools": sorted(set(tools)),
        "columns": [{"key": c, "label": LABELS.get(c, c.replace("_", " "))} for c in args.columns],
        "rows": [[_cell(r.get(c), c) for c in args.columns] for r in shown],
        "total_rows": len(rows),
    }
    # The block goes out as JSON; fail here, where the model can still hear about it
    json.dumps(block, default=str)
    cut = f" of {len(rows)}" if len(rows) > len(shown) else ""
    return Shown(block=block, summary=f"Shown to the user as table {next_id}: {len(shown)}{cut} rows.")

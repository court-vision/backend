"""
Display tools: how the agent puts tables and charts in front of the user
(docs/AI_AGENT_PLAN.md § 2-3).

The model never types a table or a chart. It calls `show_table` or `show_chart`
naming a tool result it already has ("r3"), which list in it to show, and which
columns; the server copies the values out of the stored result. So every number
in a block is a number a lookup returned, by construction, and a column the
result doesn't have is an error the model can read and fix -- never a made-up
value.

A block can join several results of the same shape (one game log per player):
their rows are concatenated, and each row can show its parent result's
top-level fields (the player's `name` beside each of his games). A chart can
then draw one series per player with `series_by`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_COLUMNS = 10
MAX_ROWS = 40
MAX_SERIES = 4
MAX_POINTS = 30
BAR_DEFAULT = 12
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
SHOW_CHART: dict[str, Any] = {
    "name": "show_chart",
    "description": (
        "Show the user a bar or line chart built from tool results you already have, like show_table: "
        "name the result(s), the list to chart (`rows`), the column for the x axis (`x`: a name for bars, "
        "a date for lines) and the numeric column(s) to plot (`y`, one series each, at most "
        f"{MAX_SERIES}), or one `y` column with `series_by` to draw one series per value of that column "
        "(several players' game logs become one line per player). Bars compare players or categories on "
        "a stat; lines show a stat over games or days and are drawn in x order. Use it when the shape "
        "matters more than the exact figures; the figures belong in show_table. The server copies the "
        "values: never describe the numbers yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "result_ids": {"type": "array", "items": {"type": "string"},
                           "description": "One or more result_ids, e.g. ['r3'] or ['r4', 'r5', 'r6']"},
            "rows": {"type": "string", "description": "Key of the list to chart; may be omitted when "
                                                      "the result holds exactly one list"},
            "kind": {"type": "string", "enum": ["bar", "line"],
                     "description": "bar compares the x values; line shows a stat over time"},
            "x": {"type": "string", "description": "The column for the categories (bar) or the time axis "
                                                   "(line), e.g. 'name' or 'date'"},
            "y": {"type": "array", "items": {"type": "string"},
                  "description": f"Numeric columns to plot, one series each, at most {MAX_SERIES}; "
                                 "exactly one when series_by is set"},
            "series_by": {"type": "string",
                          "description": f"A column whose values become the series (at most {MAX_SERIES}), "
                                         "e.g. 'name' across several players' game logs"},
            "sort_by": {"type": "string", "description": "A column to order bars by; lines always run in x order"},
            "descending": {"type": "boolean", "description": "Sort high to low (default true)"},
            "limit": {"type": "integer",
                      "description": f"Most x values to show, 1-{MAX_POINTS} (default {BAR_DEFAULT} for bars, "
                                     f"{MAX_POINTS} for lines; a line keeps its latest points)"},
            "title": {"type": "string", "description": "A short title, e.g. 'Blocks per game, last 10'"},
        },
        "required": ["result_ids", "kind", "x", "y", "title"],
        "additionalProperties": False,
    },
}
DISPLAY_TOOL_NAMES = frozenset({SHOW_TABLE["name"], SHOW_CHART["name"]})

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


class _ShowChartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_ids: list[str] = Field(..., min_length=1, max_length=12)
    rows: Optional[str] = Field(None, max_length=60)
    kind: Literal["bar", "line"]
    x: str = Field(..., min_length=1, max_length=60)
    y: list[str] = Field(..., min_length=1, max_length=MAX_SERIES)
    series_by: Optional[str] = Field(None, max_length=60)
    sort_by: Optional[str] = Field(None, max_length=60)
    descending: bool = True
    limit: Optional[int] = Field(None, ge=1, le=MAX_POINTS)
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


def _validate(model: type[BaseModel], raw_input: Any) -> Any:
    try:
        return model.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        raise DisplayError(f"Invalid input: {problems}") from exc


def _collect(result_ids: list[str], path: Optional[str], results: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows of the named results, joined, and the lookups they came from."""
    rows: list[dict[str, Any]] = []
    tools: list[str] = []
    for rid in result_ids:
        stored = results.get(rid) if _RESULT_ID.match(rid) else None
        if stored is None:
            raise DisplayError(f"No result {rid}: use a result_id a lookup returned")
        rows.extend(_rows_of(stored["data"], path, rid))
        tools.append(stored["tool"])
    if not rows:
        raise DisplayError("Those results have no rows to show")
    return rows, tools


def _require_columns(rows: list[dict[str, Any]], columns: list[str]) -> None:
    known = set().union(*(row.keys() for row in rows))
    missing = [c for c in columns if c not in known]
    if missing:
        raise DisplayError(f"No column {', '.join(repr(c) for c in missing)}; the rows have: {', '.join(sorted(known))}")


def _sorted(rows: list[dict[str, Any]], by: str, descending: bool) -> list[dict[str, Any]]:
    """Rows ordered by one column, with the rows that lack it last either way."""
    present = [r for r in rows if r.get(by) is not None]
    absent = [r for r in rows if r.get(by) is None]
    return sorted(present, key=lambda r: _sort_key(r[by]), reverse=descending) + absent


def _label_of(column: str) -> str:
    return LABELS.get(column, column.replace("_", " "))


def show_table(raw_input: Any, results: dict[str, Any], next_id: str) -> Shown:
    """Build one table block from stored results, or raise DisplayError."""
    args = _validate(_ShowTableInput, raw_input)
    rows, tools = _collect(args.result_ids, args.rows, results)
    _require_columns(rows, [*args.columns, *([args.sort_by] if args.sort_by else [])])

    if args.sort_by:
        rows = _sorted(rows, args.sort_by, args.descending)
    shown = rows[: args.limit]
    block = {
        "type": "table",
        "id": next_id,
        "title": args.title,
        "result_ids": args.result_ids,
        "tools": sorted(set(tools)),
        "columns": [{"key": c, "label": _label_of(c)} for c in args.columns],
        "rows": [[_cell(r.get(c), c) for c in args.columns] for r in shown],
        "total_rows": len(rows),
    }
    # The block goes out as JSON; fail here, where the model can still hear about it
    json.dumps(block, default=str)
    cut = f" of {len(rows)}" if len(rows) > len(shown) else ""
    return Shown(block=block, summary=f"Shown to the user as table {next_id}: {len(shown)}{cut} rows.")



# ---- charts ----

def _point(value: Any, column: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DisplayError(f"'{column}' is not a numeric column; chart a number such as pts or blk")
    return value


def _category(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def show_chart(raw_input: Any, results: dict[str, Any], next_id: str) -> Shown:
    """Build one bar or line chart block from stored results, or raise DisplayError.

    Bars keep the lookup's order unless `sort_by` says otherwise and show the first
    `limit` x values; lines always run along x and keep the latest `limit` points.
    With `series_by`, one series per distinct value of that column, aligned on x.
    `total_rows` counts the x values before the limit, so the block can say
    "12 of 30" whichever way the series were made.
    """
    args = _validate(_ShowChartInput, raw_input)
    if args.series_by and len(args.y) != 1:
        raise DisplayError("With series_by, y is exactly one column: the series come from series_by")
    rows, tools = _collect(args.result_ids, args.rows, results)
    _require_columns(rows, [args.x, *args.y, *([args.series_by] if args.series_by else []),
                            *([args.sort_by] if args.sort_by else [])])

    line = args.kind == "line"
    if line:
        rows = _sorted(rows, args.x, descending=False)
    elif args.sort_by:
        rows = _sorted(rows, args.sort_by, args.descending)
    total = len(rows)
    limit = args.limit or (MAX_POINTS if line else BAR_DEFAULT)

    def cut(items: list[Any]) -> list[Any]:
        return items[-limit:] if line else items[:limit]

    if args.series_by:
        y = args.y[0]
        categories: list[str] = []
        groups: dict[str, dict[str, Optional[float]]] = {}
        for r in rows:
            category = _category(r.get(args.x))
            if category not in categories:
                categories.append(category)
            groups.setdefault(_category(r.get(args.series_by)), {})[category] = _point(r.get(y), y)
        if len(groups) > MAX_SERIES:
            raise DisplayError(f"'{args.series_by}' has {len(groups)} values and a chart holds at most "
                               f"{MAX_SERIES} series; narrow the rows or use show_table")
        total = len(categories)
        categories = cut(categories)
        series = [{"key": key, "label": key, "values": [points.get(c) for c in categories]}
                  for key, points in groups.items()]
    else:
        shown = cut(rows)
        categories = [_category(r.get(args.x)) for r in shown]
        series = [{"key": col, "label": _label_of(col), "values": [_point(r.get(col), col) for r in shown]}
                  for col in args.y]

    if len(categories) < 2:
        raise DisplayError("A chart needs at least two x values; say a single number in prose")
    if all(v is None for s in series for v in s["values"]):
        raise DisplayError("Those rows have no values to chart")

    block = {
        "type": "chart",
        "id": next_id,
        "title": args.title,
        "kind": args.kind,
        "result_ids": args.result_ids,
        "tools": sorted(set(tools)),
        "x": {"key": args.x, "label": _label_of(args.x)},
        "categories": categories,
        "series": series,
        "total_rows": total,
    }
    json.dumps(block, default=str)
    what = ", ".join(s["label"] for s in series)
    cut_note = f" of {total}" if total > len(categories) else ""
    return Shown(block=block, summary=f"Shown to the user as chart {next_id}: {args.kind} chart of {what} "
                                      f"over {len(categories)}{cut_note} x values.")

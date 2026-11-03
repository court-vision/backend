"""
query_stats, end to end: fill the spec's defaults, compile it, run it on the
reader, and shape the rows for the agent (docs/QUERY_STATS_PLAN.md).

The result is an ordinary tool result: `rows` the display tools can table or
chart, `labels` for the columns the display tools do not know, and a note
when something the spec asked for has no data. Game rows carry `series`, a
name for show_chart's series_by that stays unique when the result spans
seasons or season types, so two games never land on one point.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from core.errors import AppError
from core.nba_calendar import nba_date_et
from services.ai.stats.catalog import FPTS_NOTE, GAMES_VIEW
from services.ai.stats.compiler import ROW_CAP, StatsSpec, compile_spec
from services.ai.stats.reader import StatsReader, get_reader

COVERAGE_TTL_S = 3600.0


class SpecError(AppError):
    """A spec the compiler cannot serve as it stands; the message is the fix."""

    status_code = 422
    error_code = "STATS_SPEC"
    log_level = "info"


@dataclass(frozen=True)
class Coverage:
    first: Optional[str]
    last: Optional[str]


_coverage: tuple[Coverage, float] | None = None


def reset_coverage_cache() -> None:
    global _coverage
    _coverage = None


async def coverage(reader: StatsReader) -> Coverage:
    """The first and last season with games, cached for an hour."""
    global _coverage
    now = time.monotonic()
    if _coverage is not None and now - _coverage[1] < COVERAGE_TTL_S:
        return _coverage[0]
    rows = await reader.query(f"SELECT MIN(season) AS first, MAX(season) AS last FROM {GAMES_VIEW}")
    found = Coverage(rows[0]["first"], rows[0]["last"]) if rows else Coverage(None, None)
    _coverage = (found, now)
    return found


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _series(rows: list[dict[str, Any]]) -> None:
    seasons = {r["season"] for r in rows}
    types = {r["season_type"] for r in rows}
    for r in rows:
        parts = [r["name"]]
        if len(seasons) > 1:
            parts.append(r["season"])
        if len(types) > 1:
            parts.append(r["season_type"].replace("_", "-"))
        r["series"] = " · ".join(parts)


def _missing(spec: StatsSpec, seasons: Optional[list[str]], rows: list[dict[str, Any]]) -> list[str]:
    """Players and seasons the spec asked for that returned nothing."""
    if not spec.players:
        return []
    present_players = {r["player_id"] for r in rows}
    kind = "" if spec.season_type == "all" else spec.season_type.replace("_", "-") + " "
    where = f" in {', '.join(seasons)}" if seasons else ""
    notes = []
    absent = [p for p in spec.players if p not in present_players]
    if absent:
        notes.append(f"No {kind}games{where} for player_id {', '.join(map(str, absent))}")
    if seasons and len(seasons) > 1 and rows and "season" in rows[0]:
        present = {(r["player_id"], r["season"]) for r in rows}
        for p in spec.players:
            if p in present_players:
                gaps = [s for s in seasons if (p, s) not in present]
                if gaps:
                    notes.append(f"player_id {p} has no games in {', '.join(gaps)}")
    return notes


async def run_query_stats(spec: StatsSpec, *, reader: StatsReader | None = None,
                          today: Optional[date] = None) -> dict[str, Any]:
    reader = reader or get_reader()
    today = today or nba_date_et()
    span = await coverage(reader)
    if span.last is None:
        raise SpecError(message="Court Vision has no game history loaded yet")
    if spec.seasons is not None:
        outside = [s for s in spec.seasons if not (span.first <= s <= span.last)]
        if outside:
            raise SpecError(message=f"Court Vision's game history covers {span.first} to {span.last}; "
                                    f"{', '.join(outside)} is outside it")
        seasons: Optional[list[str]] = list(spec.seasons)
    else:
        # The latest season with games, or every season for a by-season view
        seasons = None if spec.grain == "season" else [span.last]

    compiled = compile_spec(spec, seasons=seasons, today=today)
    rows = await reader.query(compiled.sql, compiled.params)
    if len(rows) > ROW_CAP:
        raise SpecError(message=f"Over {ROW_CAP} rows. Narrow it: a window (last_games), fewer players, "
                                "fewer seasons, or grain total")
    rows = [{k: _plain(v) for k, v in r.items()} for r in rows]
    if spec.grain == "game" and rows:
        _series(rows)

    notes = _missing(spec, seasons, rows)
    if not rows and not notes:
        notes.append("No rows matched")
    if "fpts" in spec.metrics:
        notes.append(FPTS_NOTE)

    result: dict[str, Any] = {
        "grain": spec.grain,
        "season_type": spec.season_type,
        "seasons": seasons if seasons is not None else "all",
    }
    if spec.grain != "game":
        result["agg"] = spec.agg
    if spec.window is not None:
        result["window"] = {k: _plain(v) for k, v in spec.window.model_dump(by_alias=True, exclude_none=True).items()}
    if spec.leaders:
        result["min_games"] = spec.effective_min_games()
    result["labels"] = compiled.labels
    result["rows"] = rows
    result["total_rows"] = len(rows)
    if notes:
        result["note"] = "; ".join(notes)
    return result

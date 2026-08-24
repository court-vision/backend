"""Parse Yahoo Fantasy league settings and matchup category stats into canonical shapes.

NOTE: written from the Yahoo Fantasy Sports API documentation; the connected Yahoo
app currently returns 403 for every Fantasy endpoint, so these parsers are validated
against synthetic fixtures only until a working Yahoo connection exists.
Everything is defensive: values arrive as strings, containers may be dicts or lists.
"""

from typing import Any

import requests

from core.settings import settings
from services.scoring.models import CategoryDef, CategoryTeamScoreData, LeagueSettings, StatLine
from services.scoring.vocab import STATS, YAHOO_COMPOSITE_IDS, YAHOO_ID_TO_KEY
from utils.yahoo_helpers import normalize_position

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

YAHOO_SCORING_TYPE_MAP: dict[str, tuple[str, str | None]] = {
    "head": ("categories", "each_category"),
    "headone": ("categories", "most_categories"),
    "headpoint": ("points", None),
    "point": ("points", None),
    "roto": ("roto", None),
}


# ---- fetch -----------------------------------------------------------------

def fetch_yahoo_league_settings(access_token: str, league_key: str) -> dict:
    resp = requests.get(
        f"{YAHOO_API_BASE}/league/{league_key}/settings?format=json",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=settings.http_timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ---- helpers for Yahoo's dict-or-list nesting --------------------------------

def _is_indexed(d: dict) -> bool:
    """Yahoo collections are dicts keyed "0", "1", ... plus "count"."""
    return bool(d) and all(k == "count" or str(k).isdigit() for k in d.keys())


def _as_list(obj: Any) -> list:
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        if _is_indexed(obj):
            return [v for k, v in obj.items() if k != "count"]
        return [obj]
    return [obj]


def _merge_dicts(items: Any) -> dict:
    """Yahoo often returns [ {...}, {...} ] or [[{...}, {...}], {...}]; flatten into one dict."""
    out: dict = {}
    for item in _as_list(items):
        if isinstance(item, dict):
            out.update(item)
        elif isinstance(item, list):
            out.update(_merge_dicts(item))
    return out


def _to_float(value: Any) -> float | None:
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---- settings --------------------------------------------------------------

def parse_yahoo_settings(payload: dict[str, Any], season: int | None = None) -> LeagueSettings:
    league = payload.get("fantasy_content", payload).get("league", payload)
    merged = _merge_dicts(league)
    meta = merged
    settings_obj = _merge_dicts(merged.get("settings"))

    warnings: list[str] = []
    unsupported: list[str] = []
    raw_type = str(meta.get("scoring_type") or settings_obj.get("scoring_type") or "").lower()
    if raw_type in YAHOO_SCORING_TYPE_MAP:
        scoring_type, win_mode = YAHOO_SCORING_TYPE_MAP[raw_type]
    else:
        scoring_type, win_mode = "points", None
        warnings.append(f"unknown Yahoo scoring_type {raw_type!r}; treated as points")

    categories: list[CategoryDef] = []
    for entry in _as_list(_merge_dicts(settings_obj.get("stat_categories")).get("stats")):
        stat = entry.get("stat", entry) if isinstance(entry, dict) else {}
        sid = _to_int(stat.get("stat_id"))
        if sid is None:
            continue
        if str(stat.get("enabled", "1")) == "0" or str(stat.get("is_only_display_stat", "0")) == "1":
            continue
        key = YAHOO_ID_TO_KEY.get(sid)
        if key is None:
            unsupported.append(f"yahoo:{sid}:{stat.get('display_name', '?')}")
            continue
        d = STATS[key]
        sort_order = stat.get("sort_order")
        higher = d.higher_is_better if sort_order is None else str(sort_order) != "0"
        categories.append(CategoryDef(key=key, label=d.label, higher_is_better=higher, is_rate=d.is_rate))

    weights: dict[str, float] = {}
    for entry in _as_list(_merge_dicts(settings_obj.get("stat_modifiers")).get("stats")):
        stat = entry.get("stat", entry) if isinstance(entry, dict) else {}
        sid = _to_int(stat.get("stat_id"))
        key = YAHOO_ID_TO_KEY.get(sid) if sid is not None else None
        value = _to_float(stat.get("value"))
        if key is None:
            if sid is not None:
                unsupported.append(f"yahoo:{sid}:modifier")
            continue
        if STATS[key].is_rate:
            warnings.append(f"rate stat {key} cannot carry point weights; ignored")
            continue
        if value:
            weights[key] = value

    slots: dict[str, int] = {}
    for entry in _as_list(settings_obj.get("roster_positions")):
        rp = entry.get("roster_position", entry) if isinstance(entry, dict) else {}
        pos = normalize_position(str(rp.get("position", "")))
        count = _to_int(rp.get("count"), 0) or 0
        if pos and count:
            slots[pos] = slots.get(pos, 0) + count

    start_week = _to_int(meta.get("start_week"))
    end_week = _to_int(meta.get("end_week"))
    league_key = str(meta.get("league_key") or "")
    season_val = season or _to_int(meta.get("season")) or 0
    return LeagueSettings(
        provider="yahoo",
        provider_league_id=league_key,
        season=season_val,
        name=meta.get("name"),
        scoring_type=scoring_type,
        category_win_mode=win_mode,
        categories=categories,
        point_weights=weights,
        matchup_periods={
            "periods": {},
            "start_week": start_week,
            "end_week": end_week,
            "playoff_start_week": _to_int(settings_obj.get("playoff_start_week")),
            "period_count": (end_week - start_week + 1) if start_week and end_week else None,
        },
        roster_slots=slots,
        raw_settings={
            "scoring_type": raw_type,
            "stat_categories": settings_obj.get("stat_categories"),
            "stat_modifiers": settings_obj.get("stat_modifiers"),
            "roster_positions": settings_obj.get("roster_positions"),
            "weeks": {"start_week": start_week, "end_week": end_week,
                      "playoff_start_week": settings_obj.get("playoff_start_week")},
        },
        unsupported=unsupported,
        warnings=warnings,
    )


# ---- matchup ---------------------------------------------------------------

def statline_from_yahoo_stats(stats: Any) -> StatLine:
    """StatLine from a Yahoo `stats` list [{"stat": {"stat_id", "value"}}]; composites split into makes/attempts."""
    line = StatLine()
    names = set(StatLine.field_names())
    for entry in _as_list(stats):
        stat = entry.get("stat", entry) if isinstance(entry, dict) else {}
        sid = _to_int(stat.get("stat_id"))
        if sid is None:
            continue
        value = stat.get("value")
        if sid in YAHOO_COMPOSITE_IDS and isinstance(value, str) and "/" in value:
            made, att = value.split("/", 1)
            mk, ak = YAHOO_COMPOSITE_IDS[sid]
            setattr(line, mk, _to_float(made) or 0.0)
            setattr(line, ak, _to_float(att) or 0.0)
            continue
        key = YAHOO_ID_TO_KEY.get(sid)
        if key in names:
            setattr(line, key, _to_float(value) or 0.0)
    return line


def _team_key(team_obj: Any) -> str | None:
    return _merge_dicts(team_obj).get("team_key")


def parse_yahoo_matchup_categories(matchup: dict[str, Any], our_team_key: str,
                                   categories: list[CategoryDef] | None = None
                                   ) -> tuple[CategoryTeamScoreData, CategoryTeamScoreData] | None:
    """Per-team category totals + W/L/T from a Yahoo `matchup` dict (with `stat_winners` and per-team `team_stats`)."""
    teams_container = _merge_dicts(matchup.get("0", matchup)).get("teams") or matchup.get("teams")
    sides: dict[str, StatLine] = {}
    for team_entry in _as_list(teams_container):
        team_obj = team_entry.get("team", team_entry) if isinstance(team_entry, dict) else team_entry
        key = _team_key(team_obj)
        if not key:
            continue
        stats = _merge_dicts(_merge_dicts(team_obj).get("team_stats")).get("stats")
        sides[key] = statline_from_yahoo_stats(stats)
    if our_team_key not in sides or len(sides) < 2:
        return None
    opp_key = next(k for k in sides if k != our_team_key)

    wins = losses = ties = 0
    for entry in _as_list(matchup.get("stat_winners")):
        sw = entry.get("stat_winner", entry) if isinstance(entry, dict) else {}
        if str(sw.get("is_tied", "0")) == "1":
            ties += 1
        elif sw.get("winner_team_key") == our_team_key:
            wins += 1
        elif sw.get("winner_team_key"):
            losses += 1

    def _data(line: StatLine, w: int, l: int) -> CategoryTeamScoreData:
        keys = [c.key for c in categories] if categories else [k for k in STATS if k in line.field_names() or STATS[k].is_rate]
        totals = {k: (round(line.get(k), 4) if STATS[k].is_rate else line.get(k)) for k in keys}
        raw = {k: line.get(k) for k in ("fgm", "fga", "ftm", "fta", "fg3m", "fg3a")}
        return CategoryTeamScoreData(totals=totals, raw=raw if any(raw.values()) else None, wins=w, losses=l, ties=ties)

    return _data(sides[our_team_key], wins, losses), _data(sides[opp_key], losses, wins)

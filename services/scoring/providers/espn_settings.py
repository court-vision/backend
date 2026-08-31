"""Parse ESPN Fantasy league settings and matchup category scores into canonical shapes.

Validated against a live H2H_POINTS league payload (tests/fixtures/espn_settings_h2h_points.json).
Category-league fields are parsed defensively: unknown stat ids are reported in
`unsupported`, and a missing `cumulativeScore` yields None.
"""

from typing import Any

from services.scoring.models import CategoryDef, CategoryTeamScoreData, LeagueSettings, StatLine
from services.scoring.vocab import ESPN_ID_TO_KEY, RATE_KEYS, STATS
from utils.espn_helpers import POSITION_MAP, STATS_MAP

# ESPN player position ids (defaultPositionId space, used by rosterSettings.positionLimits).
# Not the lineup-slot ids POSITION_MAP covers.
POSITION_ID_MAP: dict[int, str] = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}

ESPN_SCORING_TYPE_MAP: dict[str, tuple[str, str | None]] = {
    "H2H_POINTS": ("points", None),
    "H2H_CATEGORY": ("categories", "each_category"),
    "H2H_MOST_CATEGORIES": ("categories", "most_categories"),
    "ROTO": ("roto", None),
    "TOTAL_SEASON_POINTS": ("points", None),
}


def parse_espn_settings(payload: dict[str, Any]) -> LeagueSettings:
    """`payload` is the full league JSON (id, seasonId, settings) or just its `settings` dict."""
    settings = payload.get("settings", payload) or {}
    scoring = settings.get("scoringSettings", {}) or {}
    raw_type = scoring.get("scoringType") or "H2H_POINTS"
    warnings: list[str] = []
    unsupported: list[str] = []

    if raw_type in ESPN_SCORING_TYPE_MAP:
        scoring_type, win_mode = ESPN_SCORING_TYPE_MAP[raw_type]
    else:
        scoring_type, win_mode = "points", None
        warnings.append(f"unknown ESPN scoringType {raw_type!r}; treated as points")

    weights: dict[str, float] = {}
    categories: list[CategoryDef] = []
    for item in scoring.get("scoringItems", []) or []:
        stat_id = item.get("statId")
        key = ESPN_ID_TO_KEY.get(int(stat_id)) if stat_id is not None else None
        if key is None:
            unsupported.append(f"espn:{stat_id}:{STATS_MAP.get(str(stat_id), '?')}")
            continue
        if scoring_type == "points":
            if key in RATE_KEYS:
                warnings.append(f"rate stat {key} cannot carry point weights; ignored")
                continue
            weights[key] = float(item.get("points") or 0)
            if item.get("pointsOverrides"):
                warnings.append(f"pointsOverrides ignored for {key}")
        else:
            d = STATS[key]
            # ESPN's isReverseItem is authoritative for "lower is better" (e.g. TO); fall back to our default.
            reverse = item.get("isReverseItem")
            higher = (not bool(reverse)) if reverse is not None else d.higher_is_better
            categories.append(CategoryDef(key=key, label=d.label, higher_is_better=higher, is_rate=d.is_rate))

    sched = settings.get("scheduleSettings", {}) or {}
    roster = settings.get("rosterSettings", {}) or {}
    slots: dict[str, int] = {}
    for slot_id, count in (roster.get("lineupSlotCounts") or {}).items():
        try:
            name = POSITION_MAP.get(int(slot_id))
        except (TypeError, ValueError):
            name = None
        if name and count:
            slots[name] = int(count)

    limits: dict[str, int] = {}
    for pos_id, cap in (roster.get("positionLimits") or {}).items():
        try:
            name = POSITION_ID_MAP.get(int(pos_id))
            cap = int(cap)
        except (TypeError, ValueError):
            continue
        # -1 = unlimited (omitted). An explicit 0 is a real "none allowed" rule and is kept.
        if name and cap >= 0:
            limits[name] = cap

    draft = settings.get("draftSettings", {}) or {}
    draft_settings = {
        "type": draft.get("type"),
        "date": draft.get("date"),
        "pick_order": draft.get("pickOrder") or [],
        "time_per_selection": draft.get("timePerSelection"),
        "keeper_count": draft.get("keeperCount"),
        "auction_budget": draft.get("auctionBudget"),
    } if draft else {}

    league_id = payload.get("id")
    season = payload.get("seasonId")
    return LeagueSettings(
        provider="espn",
        provider_league_id=str(league_id) if league_id is not None else "",
        season=int(season) if season is not None else 0,
        name=settings.get("name"),
        scoring_type=scoring_type,
        category_win_mode=win_mode,
        categories=categories,
        point_weights=weights,
        matchup_periods={
            "periods": sched.get("matchupPeriods", {}) or {},
            "period_count": sched.get("matchupPeriodCount"),
            "period_length": sched.get("matchupPeriodLength"),
            "playoff_period_length": sched.get("playoffMatchupPeriodLength"),
            "playoff_team_count": sched.get("playoffTeamCount"),
        },
        roster_slots=slots,
        position_limits=limits,
        draft_settings=draft_settings,
        raw_settings={
            "scoringSettings": scoring,
            "scheduleSettings": sched,
            "rosterSettings": roster,
            "draftSettings": draft,
        },
        unsupported=unsupported,
        warnings=warnings,
    )


def parse_espn_category_score(team_matchup: dict[str, Any]) -> CategoryTeamScoreData | None:
    """Per-team category totals from an ESPN matchup side (`schedule[].home` / `.away`).

    Expected shape: {"cumulativeScore": {"wins", "losses", "ties",
    "scoreByStat": {"<statId>": {"score": <number>, ...}}}}.
    """
    cumulative = team_matchup.get("cumulativeScore")
    if not isinstance(cumulative, dict):
        return None
    by_stat = cumulative.get("scoreByStat") or {}
    totals: dict[str, float] = {}
    raw: dict[str, float] = {}
    for stat_id, entry in by_stat.items():
        try:
            key = ESPN_ID_TO_KEY.get(int(stat_id))
        except (TypeError, ValueError):
            key = None
        if key is None:
            continue
        value = entry.get("score") if isinstance(entry, dict) else entry
        try:
            value = float(value or 0)
        except (TypeError, ValueError):
            continue
        if key in ("fgm", "fga", "ftm", "fta", "fg3m", "fg3a"):
            raw[key] = value
        totals[key] = value
    # Derive rate categories from makes/attempts when ESPN omits the percentage ids
    for key, d in STATS.items():
        if d.is_rate and key not in totals and d.numerator in raw and d.denominator in raw:
            totals[key] = round(raw[d.numerator] / raw[d.denominator], 4) if raw[d.denominator] else 0.0
    return CategoryTeamScoreData(
        totals=totals,
        raw=raw or None,
        wins=int(cumulative.get("wins") or 0),
        losses=int(cumulative.get("losses") or 0),
        ties=int(cumulative.get("ties") or 0),
    )


def statline_from_espn_stats(stats: dict[str, Any] | None) -> StatLine:
    """Build a StatLine from an ESPN `stats` / `averageStats` dict keyed by stat id."""
    line = StatLine()
    if not stats:
        return line
    names = set(StatLine.field_names())
    for stat_id, value in stats.items():
        try:
            key = ESPN_ID_TO_KEY.get(int(stat_id))
        except (TypeError, ValueError):
            key = None
        if key and key in names:
            try:
                setattr(line, key, float(value or 0))
            except (TypeError, ValueError):
                pass
    return line

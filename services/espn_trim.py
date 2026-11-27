"""
Asking ESPN's league endpoint for only what a read uses.

A roster view answers with every team's roster, each player carrying six of
ESPN's own stat splits, and the schedule view with the whole season. Most
reads show one team or one matchup, and value players from our stored stats,
so they ask ESPN to leave the rest out (probed 2026-11-27 on a public league):

- `rosterForTeamId` (repeatable): only those teams carry `roster`; every team
  still comes with mTeam's name, owners and counters. An id that is not in the
  league leaves every roster out (still a 200), so a read that names ids checks
  they came back (`rosters_included`) and asks again without the filter when
  they did not. Lineup read: 1.31 MB -> 158 KB.
- `x-fantasy-filter: {"schedule": {"filterMatchupPeriodIds": ..., "filterTeamIds": ...}}`:
  only that team's matchup in that period (mMatchup puts every team's roster on
  the current period's entries). Matchup read with two rosters: 1.58 MB -> 332 KB.
- the players filter's stat filters on `kona_player_info` (`season_totals`):
  only the season's actual totals, the one split a free agent's parsing reads
  (`Player`: `{year}_total`). 300 free agents: 2.43 MB -> 738 KB, the same
  players in the same order.

Player stat filters do nothing on mRoster: a roster's players keep every split.

The matchup read needs the opponent's id before it can name two rosters, so
the last matchup each team was read in is remembered here (`remember_pair`),
per process. It is only a hint: a read made with it is checked (`pair_holds`)
and read again in full when the period has moved on or the pairing is not the
one remembered, which is once a week per team.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

# A process holds a pair per team read since it started; far below this.
_MAX_PAIRS = 4096


def roster_params(team_ids: Iterable[int]) -> dict[str, list[int]]:
    """The query params that limit a roster view to these ESPN team ids."""
    return {"rosterForTeamId": sorted({int(i) for i in team_ids})}


def rosters_included(payload: Mapping[str, Any], team_ids: Iterable[int]) -> bool:
    """Whether every one of these teams is in the payload with its roster."""
    teams = {t.get("id"): t for t in payload.get("teams") or [] if isinstance(t, Mapping)}
    wanted = [int(i) for i in team_ids]
    return bool(wanted) and all(isinstance(teams.get(i), Mapping) and "roster" in teams[i] for i in wanted)


def schedule_filter(matchup_period_id: int, team_id: int) -> dict[str, str]:
    """The header that limits the schedule view to one team's matchup in one period."""
    return {"x-fantasy-filter": json.dumps({"schedule": {
        "filterMatchupPeriodIds": {"value": [int(matchup_period_id)]},
        "filterTeamIds": {"value": [int(team_id)]},
    }})}


def season_totals(year: int) -> dict[str, Any]:
    """Players-filter keys that keep only the season's actual totals (`00{year}`) on each player."""
    return {
        "filterStatsForExternalIds": {"value": [int(year)]},
        "filterStatsForSourceIds": {"value": [0]},       # 0 actual, 1 projected
        "filterStatsForSplitTypeIds": {"value": [0]},    # 0 season, 1-3 last 7/15/30, 5 one game
    }


# ------------------------------- the matchup's two teams ------------------------------- #


@dataclass(frozen=True)
class MatchupPair:
    matchup_period_id: int
    ours: int
    opponent: int


_pairs: dict[tuple[str, str, int], MatchupPair] = {}


def _key(league_info, ours: int) -> tuple[str, str, int]:
    return (str(league_info.year), str(league_info.league_id), int(ours))


def recall_pair(league_info) -> Optional[MatchupPair]:
    """The matchup this team was last read in, when its ESPN team id is known."""
    ours = getattr(league_info, "espn_team_id", None)
    return _pairs.get(_key(league_info, ours)) if ours is not None else None


def remember_pair(league_info, matchup_period_id: Optional[int], ours: Optional[int], opponent: Optional[int]) -> None:
    if matchup_period_id is None or ours is None or opponent is None:
        return
    if len(_pairs) >= _MAX_PAIRS:
        _pairs.clear()
    _pairs[_key(league_info, ours)] = MatchupPair(int(matchup_period_id), int(ours), int(opponent))


def forget_pair(league_info, ours: Optional[int]) -> None:
    """No matchup this period (a bye, eliminated, the offseason): the next read asks in full
    once rather than trying the old pair first every time."""
    if ours is not None:
        _pairs.pop(_key(league_info, ours), None)


def forget_pairs() -> None:
    """Tests start from an empty memory."""
    _pairs.clear()


def pair_holds(payload: Mapping[str, Any], pair: MatchupPair) -> bool:
    """Whether a read made with `pair` is the whole answer: ESPN is still in that matchup
    period, both rosters came back, and the period's schedule pairs the same two teams."""
    status = payload.get("status") or {}
    if status.get("currentMatchupPeriod") != pair.matchup_period_id:
        return False
    if not rosters_included(payload, (pair.ours, pair.opponent)):
        return False
    wanted = {pair.ours, pair.opponent}
    for matchup in payload.get("schedule") or []:
        if matchup.get("matchupPeriodId") != pair.matchup_period_id:
            continue
        sides = {(matchup.get("home") or {}).get("teamId"), (matchup.get("away") or {}).get("teamId")}
        if pair.ours in sides:
            return sides == wanted
    return False

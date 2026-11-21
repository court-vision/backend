"""The stored daily boards as a public read (the Live rankings plan, step 7).

Every morning of the season the data platform stores Court Vision's two
standard boards in nba.daily_rankings with ESPN's rank that day beside them
(`services/valuation/daily.py` builds them; the platform writes them). Until
now only the grading and the dashboard read that table. This is its public
face: the newest board with each player's move over a day and a week and
ESPN's rank beside him, and one player's rank on every stored day.

Only `source='cv'` and `source='espn'` are ever read. The table also holds
candidate boards graded in shadow (the analyst's, under another source): they
are never served from here, whatever a caller asks for, because no caller can
name a source.

A move is measured between two stored boards, so it says what the boards say:
`compared.day` is the previous stored board and `compared.week` the newest one
at least seven days back. A morning the pipeline missed leaves a gap; rather
than call a three-week-old board "a week ago", a board further back than the
tolerances below is not compared against at all and the move is null.

Everything here reads; the data platform owns the nba.* tables.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from core.errors import NotFoundError
from core.settings import settings
from db.base import db_operation
from db.models.nba.daily_rankings import (
    BOARD_CATEGORIES,
    BOARD_POINTS,
    SOURCE_CV,
    SOURCE_ESPN,
    DailyRanking,
)
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.live_rankings import (
    EspnRankAge,
    LiveBoardPlace,
    LivePlayerEspn,
    LivePlayerRankData,
    LivePlayerRankResp,
    LiveRankDay,
    LiveRankingsCompared,
    LiveRankingsData,
    LiveRankingsPlayer,
    LiveRankingsResp,
)

BOARDS = (BOARD_POINTS, BOARD_CATEGORIES)

# "A day ago" is the previous stored board, and not one older than this.
DAY_REACH = 3
# "A week ago" is the newest board at least WEEK days back, and not one older than WEEK + WEEK_REACH.
WEEK = 7
WEEK_REACH = 3


def compared_days(as_of: date, season: str) -> tuple[Optional[date], Optional[date]]:
    """The two earlier boards a move is measured against, or None where there is none near enough."""
    day = DailyRanking.latest_date(SOURCE_CV, season, before=as_of)
    if day is not None and (as_of - day).days > DAY_REACH:
        day = None
    week = DailyRanking.latest_date(SOURCE_CV, season, before=as_of - timedelta(days=WEEK - 1))
    if week is not None and (as_of - week).days > WEEK + WEEK_REACH:
        week = None
    return day, week


def espn_day(as_of: date, season: str) -> Optional[date]:
    """The day of the ESPN ranks shown beside a board: its own, or the newest before it.

    ESPN's read can fail on a morning the boards were stored; the pipeline
    retries it on its next tick, and until then the day before's ranks stand.
    """
    return DailyRanking.latest_date(SOURCE_ESPN, season, before=as_of + timedelta(days=1))


def stored_days(source: str, season: str) -> list[date]:
    """Every day of the season `source` has a board for, oldest first.

    A player's history only has the days he was ranked. Beside this list a
    reader can tell a day he was off the board from a morning no board was
    stored, which a history alone cannot.
    """
    query = (
        DailyRanking.select(DailyRanking.as_of_date)
        .where((DailyRanking.source == source) & (DailyRanking.season == season))
        .distinct()
        .order_by(DailyRanking.as_of_date)
    )
    return [row.as_of_date for row in query]


def move(then: Optional[int], now: int) -> Optional[int]:
    """Places gained from `then` to `now`; positive is an improvement."""
    return None if then is None else then - now


def _number(value) -> Optional[float]:
    return None if value is None else float(value)


def _teams(player_ids: Iterable[int]) -> dict[int, Optional[str]]:
    """Where each player plays now: the profile's team, as the draft board reads it."""
    ids = list(player_ids)
    if not ids:
        return {}
    # Raw FK values: `rec.team` would fetch a team row per player.
    return {
        rec.player_id: rec.team_id
        for rec in PlayerProfile.select(PlayerProfile.player, PlayerProfile.team).where(PlayerProfile.player.in_(ids))
    }


def espn_age(history: list[tuple[date, int]], first_stored: Optional[date]) -> Optional[EspnRankAge]:
    """How long ESPN has held a player's newest rank, from his `(day, rank)` rows, oldest first.

    The run is counted over the days ESPN's ranks were stored. One that reaches
    the first stored day is flagged: the rank is at least that old.
    """
    if not history:
        return None
    _, rank = history[-1]
    since = history[-1][0]
    for day, earlier in reversed(history):
        if earlier != rank:
            break
        since = day
    return EspnRankAge(rank=rank, unchanged_since=since, since_first_stored=since == first_stored)


class LiveRankingsService:
    @staticmethod
    @db_operation("rankings.live")
    def get_board() -> LiveRankingsResp:
        """The newest stored board: every ranked player, his moves and ESPN's rank beside him."""
        season = settings.nba_season
        as_of = DailyRanking.latest_date(SOURCE_CV, season)
        if as_of is None:
            return LiveRankingsResp(
                status=ApiStatus.SUCCESS,
                message=f"No live rankings stored for {season} yet",
                data=LiveRankingsData(season=season),
            )

        rows = list(
            DailyRanking.select(
                DailyRanking.player, DailyRanking.board, DailyRanking.rank, DailyRanking.value,
                DailyRanking.games, DailyRanking.season_gp, DailyRanking.injury_status, DailyRanking.model,
            ).where(
                (DailyRanking.as_of_date == as_of) & (DailyRanking.source == SOURCE_CV)
                & (DailyRanking.season == season)
            )
        )
        day, week = compared_days(as_of, season)
        espn_as_of = espn_day(as_of, season)
        day_ranks = {board: DailyRanking.ranks_on(day, SOURCE_CV, board) if day else {} for board in BOARDS}
        week_ranks = {board: DailyRanking.ranks_on(week, SOURCE_CV, board) if week else {} for board in BOARDS}
        espn_ranks = {
            board: DailyRanking.ranks_on(espn_as_of, SOURCE_ESPN, board) if espn_as_of else {} for board in BOARDS
        }

        ids = {row.player_id for row in rows}
        identity = {
            rec.id: rec
            for rec in Player.select(Player.id, Player.name, Player.position).where(Player.id.in_(list(ids)))
        } if ids else {}
        teams = _teams(ids)

        places: dict[int, dict[str, LiveBoardPlace]] = {}
        facts: dict[int, DailyRanking] = {}
        for row in rows:
            pid = row.player_id
            rank = int(row.rank)
            places.setdefault(pid, {})[row.board] = LiveBoardPlace(
                rank=rank,
                value=_number(row.value),
                move_1d=move(day_ranks.get(row.board, {}).get(pid), rank),
                move_7d=move(week_ranks.get(row.board, {}).get(pid), rank),
                espn_rank=espn_ranks.get(row.board, {}).get(pid),
            )
            # What the board knew about him is the same on both boards; the points row speaks for it.
            if row.board == BOARD_POINTS or pid not in facts:
                facts[pid] = row

        players = [
            LiveRankingsPlayer(
                player_id=pid,
                name=identity[pid].name,
                team=teams.get(pid),
                position=identity[pid].position,
                injury_status=facts[pid].injury_status,
                season_gp=facts[pid].season_gp,
                games=_number(facts[pid].games),
                points=by_board.get(BOARD_POINTS),
                categories=by_board.get(BOARD_CATEGORIES),
            )
            for pid, by_board in places.items()
            if pid in identity
        ]
        unranked = len(players) + 1
        players.sort(key=lambda p: (p.points.rank if p.points else unranked, p.player_id))

        return LiveRankingsResp(
            status=ApiStatus.SUCCESS,
            message="Court Vision live rankings",
            data=LiveRankingsData(
                season=season,
                as_of=as_of,
                model=next((row.model for row in rows if row.model), None),
                espn_as_of=espn_as_of,
                compared=LiveRankingsCompared(day=day, week=week),
                players=players,
            ),
        )

    @staticmethod
    @db_operation("rankings.live.player")
    def get_player(player_id: int) -> LivePlayerRankResp:
        """One player's place on the newest board and his rank on every stored day of the season."""
        player = Player.get_or_none(Player.id == player_id)
        if player is None:
            raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")
        season = settings.nba_season
        team = _teams([player_id]).get(player_id)
        board_days = stored_days(SOURCE_CV, season)
        espn_days = stored_days(SOURCE_ESPN, season)
        as_of = board_days[-1] if board_days else None

        rows = list(
            DailyRanking.select(
                DailyRanking.as_of_date, DailyRanking.source, DailyRanking.board, DailyRanking.rank,
                DailyRanking.value, DailyRanking.games, DailyRanking.season_gp, DailyRanking.injury_status,
                DailyRanking.model,
            ).where(
                (DailyRanking.player == player_id) & (DailyRanking.season == season)
                & (DailyRanking.source.in_([SOURCE_CV, SOURCE_ESPN]))
            ).order_by(DailyRanking.as_of_date)
        )
        if as_of is None or not rows:
            return LivePlayerRankResp(
                status=ApiStatus.SUCCESS,
                message=f"No live rankings stored for this player in {season}",
                data=LivePlayerRankData(
                    player_id=player.id, name=player.name, team=team, position=player.position,
                    season=season, as_of=as_of, board_days=board_days, espn_days=espn_days,
                ),
            )

        ranks: dict[tuple[str, str], dict[date, int]] = {}
        today: dict[str, DailyRanking] = {}
        for row in rows:
            ranks.setdefault((row.source, row.board), {})[row.as_of_date] = int(row.rank)
            if row.source == SOURCE_CV and row.as_of_date == as_of:
                today[row.board] = row

        day, week = compared_days(as_of, season)
        espn_as_of = espn_day(as_of, season)
        first_espn = espn_days[0] if espn_days else None

        def espn_now(board: str) -> Optional[EspnRankAge]:
            held = ranks.get((SOURCE_ESPN, board), {})
            # A rank ESPN no longer gives him is not his rank: only the newest read counts.
            if espn_as_of is None or espn_as_of not in held:
                return None
            return espn_age(sorted(held.items()), first_espn)

        espn = LivePlayerEspn(as_of=espn_as_of, points=espn_now(BOARD_POINTS), categories=espn_now(BOARD_CATEGORIES))

        def place(board: str) -> Optional[LiveBoardPlace]:
            row = today.get(board)
            if row is None:
                return None
            held = ranks.get((SOURCE_CV, board), {})
            rank = int(row.rank)
            age = espn.points if board == BOARD_POINTS else espn.categories
            return LiveBoardPlace(
                rank=rank,
                value=_number(row.value),
                move_1d=move(held.get(day) if day else None, rank),
                move_7d=move(held.get(week) if week else None, rank),
                espn_rank=age.rank if age else None,
            )

        days = sorted({stored for held in ranks.values() for stored in held})
        history = [
            LiveRankDay(
                as_of=stored,
                points=ranks.get((SOURCE_CV, BOARD_POINTS), {}).get(stored),
                categories=ranks.get((SOURCE_CV, BOARD_CATEGORIES), {}).get(stored),
                espn_points=ranks.get((SOURCE_ESPN, BOARD_POINTS), {}).get(stored),
                espn_categories=ranks.get((SOURCE_ESPN, BOARD_CATEGORIES), {}).get(stored),
            )
            for stored in days
        ]
        fact = today.get(BOARD_POINTS) or today.get(BOARD_CATEGORIES)

        return LivePlayerRankResp(
            status=ApiStatus.SUCCESS,
            message="Court Vision live rankings for one player",
            data=LivePlayerRankData(
                player_id=player.id,
                name=player.name,
                team=team,
                position=player.position,
                season=season,
                as_of=as_of,
                model=fact.model if fact else None,
                compared=LiveRankingsCompared(day=day, week=week),
                injury_status=fact.injury_status if fact else None,
                season_gp=fact.season_gp if fact else None,
                games=_number(fact.games) if fact else None,
                points=place(BOARD_POINTS),
                categories=place(BOARD_CATEGORIES),
                espn=espn,
                history=history,
                board_days=board_days,
                espn_days=espn_days,
            ),
        )

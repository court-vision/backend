from datetime import datetime, date as date_type, timedelta

from core.errors import NotFoundError
from core.logging import get_logger
from core.settings import settings
from services.espn_service import EspnService
from services.matchup_days import (
    build_day, comparison_to_schema, make_nba_id_resolver, nba_today as _nba_today, score_stat_row,
)
from services.scoring.models import CategoryTeamScoreData, StatLine
from services.scoring.resolver import resolve_scoring_for_league_info, resolve_scoring_for_team
from schemas.matchup import CategoryTeamScore
from services.yahoo_service import YahooService
from services.team_service import TeamService
from schemas.matchup import (
    MatchupResp,
    MatchupScoreHistoryResp,
    MatchupScoreHistory,
    DailyScorePoint,
    LiveMatchupResp,
    LiveMatchupData,
    LiveMatchupTeam,
    LiveMatchupPlayer,
    PlayerLiveStats,
    DailyMatchupResp,
    DailyMatchupData,
    DailyMatchupTeam,
    DailyMatchupPlayerStats,
    DailyMatchupFuturePlayer,
    WeeklyMatchupResp,
    WeeklyMatchupData,
    SeasonSummaryResp,
    SeasonSummaryData,
    WeekResult,
)
from schemas.common import ApiStatus, LeagueInfo, FantasyProvider
from db.models.stats.daily_matchup_score import DailyMatchupScore
from services.matchup_window import (
    DayWatermark, MatchupWindow, WatermarkSource, resolve_matchup_window,
)

log = get_logger("matchup")


class MatchupService:
    """Service for handling matchup-related operations."""

    @staticmethod
    async def get_current_matchup(
        league_info: LeagueInfo,
        avg_window: str = "season"
    ) -> MatchupResp:
        """
        Get current matchup data for a team using league credentials.

        Args:
            league_info: League credentials and team information
            avg_window: Averaging window for projections (season, last_7, last_14, last_30)

        Returns:
            MatchupResp with matchup data or error
        """
        scoring = resolve_scoring_for_league_info(league_info)
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window, scoring=scoring)
        return await EspnService.get_matchup_data(league_info, avg_window, scoring=scoring)

    @staticmethod
    async def get_matchup_by_team_id(
        user_id: int,
        team_id: int,
        avg_window: str = "season",
    ) -> MatchupResp:
        """
        Get current matchup data for a saved team.

        Args:
            user_id: The user's ID (for authorization)
            team_id: The saved team's ID
            avg_window: Averaging window for projections

        Returns:
            MatchupResp with matchup data; SUCCESS with data=None when there is no current
            matchup (bye week / offseason). Provider failures raise typed AppErrors.
        """
        # The team's stored league info (view_team itself raises TEAM_NOT_FOUND for an unknown id)
        team_resp = await TeamService.view_team(team_id)
        if team_resp.status != ApiStatus.SUCCESS or not team_resp.data:
            raise NotFoundError("TEAM_NOT_FOUND", f"Team with ID {team_id} not found")

        league_info = team_resp.data.league_info

        # Fetch matchup data using the league info - route by provider
        # Pass team_id for Yahoo so tokens can be refreshed and persisted
        scoring = resolve_scoring_for_team(team_id)
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window, team_id, scoring=scoring)
        return await EspnService.get_matchup_data(league_info, avg_window, scoring=scoring)

    @staticmethod
    def _watermark_source(value) -> WatermarkSource:
        """Tolerant of legacy rows and of any value a future writer invents."""
        try:
            return WatermarkSource(value or "unknown")
        except ValueError:
            return WatermarkSource.UNKNOWN

    @staticmethod
    def _watermark_window(md, baseline) -> MatchupWindow:
        """Resolve the overlay day from the stored watermarks."""
        from services.schedule_service import date_for_espn_scoring_period

        stored = None
        if baseline is not None:
            stored = DayWatermark(
                baseline.scoring_period_id,
                MatchupService._watermark_source(baseline.scoring_period_source),
            )
        return resolve_matchup_window(
            provider=DayWatermark(
                md.scoring_period_id,
                MatchupService._watermark_source(md.scoring_period_source),
            ),
            baseline=stored,
            period_start=date_type.fromisoformat(md.matchup_period_start) if md.matchup_period_start else None,
            period_end=date_type.fromisoformat(md.matchup_period_end) if md.matchup_period_end else None,
            day_to_date=date_for_espn_scoring_period,
            fallback_today=_nba_today(),
        )

    @staticmethod
    def _legacy_window(md, baseline) -> MatchupWindow:
        """The wall-clock rule the watermark replaces, kept for shadow comparison.

        It decides from `baseline.date` -- a US/Central snapshot label written
        with no cutoff, so it rolls at 1 AM ET -- against a 6 AM ET "today".
        That five-hour disagreement is what dropped a full day off the score
        whenever ESPN's batch ran late.
        """
        today = _nba_today()
        game_date = baseline.date if (baseline is not None and baseline.date > today) else today
        excludes_today = baseline.date <= today if baseline is not None else False
        include_live = False
        if excludes_today and md.matchup_period_start and md.matchup_period_end:
            mp_start = date_type.fromisoformat(md.matchup_period_start)
            mp_end = date_type.fromisoformat(md.matchup_period_end)
            include_live = mp_start <= game_date <= mp_end
        return MatchupWindow(
            include_live=include_live,
            overlay_date=game_date if include_live else None,
            display_date=game_date,
            seed_zero_baseline=False,
            stale_days=0,
            reason="legacy_wall_clock",
        )

    @staticmethod
    async def get_live_matchup_by_team_id(
        user_id: int,
        team_id: int,
    ) -> LiveMatchupResp:
        """
        Get the current matchup augmented with live in-game stats per player.

        Fetches the current matchup (ESPN/Yahoo scores + roster) then overlays
        live stats from live_player_stats for each roster player matched by name.
        """
        from db.models.nba.live_player_stats import LivePlayerStats as LiveStatsModel

        # Step 1: Fetch the provider — always the default (current) scoring
        # period. We trust whatever it returns as the current roster and lineup.
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return LiveMatchupResp(status=matchup.status, message=matchup.message, data=None)

        espn_matchup_period = matchup.data.matchup_period

        # Step 2: Newest baseline snapshot for this matchup period. Ordering by
        # date still picks the newest row — watermarks are monotone, and
        # ordering by watermark would tie on days with no games.
        baseline = (
            DailyMatchupScore
            .select()
            .where(
                (DailyMatchupScore.team_id == team_id) &
                (DailyMatchupScore.matchup_period == espn_matchup_period)
            )
            .order_by(DailyMatchupScore.date.desc())
            .first()
        )

        # Step 3: Which single day of live stats belongs on top of that baseline.
        # Both rules are evaluated so disagreements surface before the flag is
        # flipped; services/matchup_window.py explains why the watermark rule
        # cannot double-count.
        window = MatchupService._watermark_window(matchup.data, baseline)
        legacy = MatchupService._legacy_window(matchup.data, baseline)
        if (window.include_live, window.overlay_date) != (legacy.include_live, legacy.overlay_date):
            log.warning(
                "matchup_window_divergence",
                team_id=team_id,
                matchup_period=espn_matchup_period,
                watermark_include_live=window.include_live,
                watermark_overlay=str(window.overlay_date),
                watermark_reason=window.reason,
                legacy_include_live=legacy.include_live,
                legacy_overlay=str(legacy.overlay_date),
            )
        if not settings.live_window_from_watermark:
            window = legacy

        game_date = window.display_date
        overlay_date = window.overlay_date
        include_live = window.include_live

        if window.seed_zero_baseline:
            # First day of a matchup period with no snapshot yet. H2H scores
            # reset each period, so a baseline "through day 0" is exactly zero.
            your_base = opponent_base = 0.0
        elif baseline is not None:
            your_base = float(baseline.current_score)
            opponent_base = float(baseline.opponent_current_score)
        else:
            your_base = matchup.data.your_team.current_score
            opponent_base = matchup.data.opponent_team.current_score

        if include_live:
            all_espn_ids = [
                p.player_id for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster
            ]
            live_stats_list = LiveStatsModel.get_live_stats_by_espn_ids(all_espn_ids, overlay_date)
            espn_id_to_live = {stat.player.espn_id: stat for stat in live_stats_list}

            # Name-based fallback for players without espn_id mapping
            unresolved_names = [
                p.name for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster
                if p.player_id not in espn_id_to_live
            ]
            name_to_live: dict[str, object] = {}
            if unresolved_names:
                fallback_stats = LiveStatsModel.get_live_stats_by_names(unresolved_names, overlay_date)
                name_to_live = {stat.player.name_normalized: stat for stat in fallback_stats}
        else:
            espn_id_to_live = {}
            name_to_live = {}

        scoring = resolve_scoring_for_team(team_id)
        live_rows: dict[int, object] = {}   # roster player_id -> live stat row (for scoring)

        def build_live_roster(roster) -> list[LiveMatchupPlayer]:
            result = []
            for p in roster:
                stat = espn_id_to_live.get(p.player_id) or name_to_live.get(p.name.lower().strip())
                live_overlay = None
                if stat:
                    live_rows[p.player_id] = stat
                    # Read-side staleness defense: if game_status is still 2
                    # but last_updated is older than 90 minutes, treat as final.
                    # Display-level safeguard only — does not modify the DB.
                    effective_status = stat.game_status
                    effective_clock = stat.game_clock
                    if stat.game_status == 2 and stat.last_updated:
                        staleness = datetime.utcnow() - stat.last_updated
                        if staleness.total_seconds() > 90 * 60:
                            effective_status = 3
                            effective_clock = None

                    live_overlay = PlayerLiveStats(
                        nba_player_id=stat.player_id,
                        live_fpts=score_stat_row(stat, scoring),
                        live_pts=stat.pts,
                        live_reb=stat.reb,
                        live_ast=stat.ast,
                        live_stl=stat.stl,
                        live_blk=stat.blk,
                        live_tov=stat.tov,
                        live_min=stat.min,
                        live_fgm=stat.fgm or 0,
                        live_fga=stat.fga or 0,
                        live_fg3m=stat.fg3m or 0,
                        live_fg3a=stat.fg3a or 0,
                        live_ftm=stat.ftm or 0,
                        live_fta=stat.fta or 0,
                        game_status=effective_status,
                        period=stat.period if effective_status == 2 else None,
                        game_clock=effective_clock,
                        last_updated=stat.last_updated.isoformat() if stat.last_updated else None,
                    )
                result.append(LiveMatchupPlayer(**p.model_dump(), live=live_overlay))
            return result

        def active_live_rows(live_roster: list[LiveMatchupPlayer]) -> list:
            """Tonight's live rows for active (non-bench/IR) players whose game has started."""
            return [
                live_rows[p.player_id]
                for p in live_roster
                if p.lineup_slot not in ("BE", "IR")
                and p.live is not None
                and p.live.game_status >= 2
                and p.player_id in live_rows
            ]

        def compute_live_score(base: float, live_roster: list[LiveMatchupPlayer]) -> float:
            """
            Add today's live points (league weights) for active roster players on top of
            the pipeline baseline. Both in-progress and final games count because the
            baseline is a morning snapshot captured before any of today's games.
            """
            today_fpts = sum(score_stat_row(row, scoring) for row in active_live_rows(live_roster))
            return round(base + today_fpts, 2)

        your_live_roster = build_live_roster(matchup.data.your_team.roster)
        opponent_live_roster = build_live_roster(matchup.data.opponent_team.roster)

        your_categories = opp_categories = None
        category_comparison = None
        if scoring.is_categories and scoring.categories is not None:
            cats = scoring.categories

            def base_categories(side: str, provider_side) -> CategoryTeamScoreData:
                # The baseline query is scoped to this matchup period, so a
                # previous period's totals can never leak in here. On a seeded
                # period start `baseline` is None and this falls through to the
                # provider's own totals, which are zero before any game in the
                # period has been played — the category equivalent of the 0.0
                # points base above.
                snap = baseline.category_scores if (baseline is not None and baseline.category_scores) else None
                if snap and isinstance(snap.get(side), dict):
                    totals = {k: v for k, v in snap[side].items() if k in cats.keys}
                    raw = {k: snap[side][k] for k in ("fgm", "fga", "ftm", "fta", "fg3m", "fg3a") if k in snap[side]}
                    return CategoryTeamScoreData(totals=totals, raw=raw or None)
                if provider_side is not None:
                    return CategoryTeamScoreData(totals=dict(provider_side.totals), raw=dict(provider_side.raw) if provider_side.raw else None)
                return CategoryTeamScoreData(totals={})

            your_base_cat = base_categories("you", matchup.data.your_team.categories)
            opp_base_cat = base_categories("opp", matchup.data.opponent_team.categories)
            if include_live:
                your_base_cat = cats.overlay(your_base_cat, StatLine.sum(StatLine.from_row(r) for r in active_live_rows(your_live_roster)))
                opp_base_cat = cats.overlay(opp_base_cat, StatLine.sum(StatLine.from_row(r) for r in active_live_rows(opponent_live_roster)))
            cmp = cats.compare(your_base_cat.totals, opp_base_cat.totals)
            your_base_cat.wins, your_base_cat.losses, your_base_cat.ties = cmp.wins, cmp.losses, cmp.ties
            opp_base_cat.wins, opp_base_cat.losses, opp_base_cat.ties = cmp.losses, cmp.wins, cmp.ties
            category_comparison = comparison_to_schema(cmp)
            your_categories = CategoryTeamScore(**your_base_cat.__dict__)
            opp_categories = CategoryTeamScore(**opp_base_cat.__dict__)
            your_live_score, opp_live_score = float(cmp.wins), float(cmp.losses)
        else:
            your_live_score = compute_live_score(your_base, your_live_roster)
            opp_live_score = compute_live_score(opponent_base, opponent_live_roster)

        your_team = LiveMatchupTeam(
            team_name=matchup.data.your_team.team_name,
            team_id=matchup.data.your_team.team_id,
            current_score=your_live_score,
            projected_score=matchup.data.your_team.projected_score,
            roster=your_live_roster,
            categories=your_categories,
        )
        opponent_team = LiveMatchupTeam(
            team_name=matchup.data.opponent_team.team_name,
            team_id=matchup.data.opponent_team.team_id,
            current_score=opp_live_score,
            projected_score=matchup.data.opponent_team.projected_score,
            roster=opponent_live_roster,
            categories=opp_categories,
        )

        return LiveMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Live matchup data fetched successfully",
            data=LiveMatchupData(
                matchup_period=matchup.data.matchup_period,
                matchup_period_start=matchup.data.matchup_period_start,
                matchup_period_end=matchup.data.matchup_period_end,
                your_team=your_team,
                opponent_team=opponent_team,
                projected_winner=matchup.data.projected_winner,
                projected_margin=matchup.data.projected_margin,
                game_date=str(game_date),
                baseline_stale_days=window.stale_days,
                scoring_format=scoring.format,
                settings_synced=scoring.settings_synced,
                category_comparison=category_comparison,
            ),
        )

    @staticmethod
    async def get_score_history(
        team_id: int,
        matchup_period: int | None = None
    ) -> MatchupScoreHistoryResp:
        """
        Get daily score history for a team's matchup period.

        Args:
            team_id: The team's ID
            matchup_period: Specific matchup period (week). If None, returns current/latest.

        Returns:
            MatchupScoreHistoryResp with daily score snapshots for charting

        Raises:
            NotFoundError (404 SCORE_HISTORY_NOT_FOUND) when nothing has been recorded yet;
            the frontend treats that 404 as "no history" (nullOn404).
        """
        query = (
            DailyMatchupScore
            .select()
            .where(DailyMatchupScore.team_id == team_id)
        )

        if matchup_period is not None:
            query = query.where(DailyMatchupScore.matchup_period == matchup_period)
        else:
            # Get the latest matchup period for this team
            latest = (
                DailyMatchupScore
                .select(DailyMatchupScore.matchup_period)
                .where(DailyMatchupScore.team_id == team_id)
                .order_by(DailyMatchupScore.matchup_period.desc())
                .limit(1)
                .first()
            )
            if not latest:
                raise NotFoundError("SCORE_HISTORY_NOT_FOUND", "No score history found for this team")
            query = query.where(DailyMatchupScore.matchup_period == latest.matchup_period)

        records = list(query.order_by(DailyMatchupScore.day_of_matchup.asc()))

        if not records:
            raise NotFoundError("SCORE_HISTORY_NOT_FOUND", "No score history found for this matchup period")

        first_record = records[0]
        history = [
            DailyScorePoint(
                date=record.date.isoformat(),
                day_of_matchup=record.day_of_matchup,
                your_score=float(record.current_score),
                opponent_score=float(record.opponent_current_score),
                your_categories=(record.category_scores or {}).get("you") if record.category_scores else None,
                opponent_categories=(record.category_scores or {}).get("opp") if record.category_scores else None,
            )
            for record in records
        ]
        history_format = "categories" if any(r.category_scores for r in records) else "points"

        return MatchupScoreHistoryResp(
            status=ApiStatus.SUCCESS,
            message="Score history retrieved successfully",
            data=MatchupScoreHistory(
                team_id=team_id,
                team_name=first_record.team_name,
                opponent_team_name=first_record.opponent_team_name,
                matchup_period=first_record.matchup_period,
                history=history,
                scoring_format=history_format,
            )
        )


    @staticmethod
    async def get_daily_matchup(
        user_id: int,
        team_id: int,
        target_date: date_type,
    ) -> DailyMatchupResp:
        """
        Get daily drill-down for a single day within a matchup period.

        For past dates: returns player box score stats from player_game_stats.
        For future dates: returns which players have games scheduled.
        """
        import pytz
        from db.models.nba.players import Player
        from db.models.nba.player_game_stats import PlayerGameStats
        from db.models.nba.games import Game

        # 1. Fetch current matchup to get both rosters and matchup period
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return DailyMatchupResp(status=matchup.status, message=matchup.message, data=None)

        md = matchup.data
        if not md.matchup_period_start or not md.matchup_period_end:
            return DailyMatchupResp(
                status=ApiStatus.NOT_FOUND,
                message="Matchup period dates unavailable — schedule may not cover current playoff period",
                data=None,
            )
        period_start = date_type.fromisoformat(md.matchup_period_start)
        period_end = date_type.fromisoformat(md.matchup_period_end)

        # 2. Validate date is within matchup period
        if target_date < period_start or target_date > period_end:
            return DailyMatchupResp(
                status=ApiStatus.BAD_REQUEST,
                message=f"Date {target_date} is outside matchup period {period_start} to {period_end}",
                data=None,
            )

        today = _nba_today()
        scoring = resolve_scoring_for_team(team_id)
        resolve = make_nba_id_resolver(md.your_team.roster + md.opponent_team.roster)
        all_roster = md.your_team.roster + md.opponent_team.roster

        games_on_date = Game.get_games_on_date(target_date)

        nba_id_to_stats: dict[int, object] = {}
        if target_date <= today:
            nba_ids = [nid for nid in (resolve(p) for p in all_roster) if nid is not None]
            if nba_ids:
                for stat in PlayerGameStats.select().where(
                    (PlayerGameStats.player_id.in_(nba_ids)) & (PlayerGameStats.game_date == target_date)
                ):
                    nba_id_to_stats[stat.player_id] = stat
                if target_date == today:
                    # Today: the nightly pipeline hasn't run, so overlay live rows for
                    # players not yet in PlayerGameStats (same source as the live endpoint).
                    from db.models.nba.live_player_stats import LivePlayerStats
                    for ls in LivePlayerStats.select().where(
                        (LivePlayerStats.player_id.in_(nba_ids)) & (LivePlayerStats.game_date == target_date)
                    ):
                        nba_id_to_stats.setdefault(ls.player_id, ls)

        day = build_day(md, target_date, today, period_start, nba_id_to_stats, games_on_date, resolve, scoring)
        return DailyMatchupResp(status=ApiStatus.SUCCESS, message="Daily matchup data fetched successfully", data=day)

    @staticmethod
    async def get_weekly_matchup(
        user_id: int,
        team_id: int,
    ) -> WeeklyMatchupResp:
        """
        Get all days in the current matchup period in a single ESPN API call.

        Makes one ESPN call, resolves player IDs once, then bulk-fetches all
        required DB data for the entire period before building per-day responses.
        This replaces N parallel getDailyMatchup calls from the frontend.
        """
        import pytz
        from db.models.nba.players import Player
        from db.models.nba.player_game_stats import PlayerGameStats
        from db.models.nba.live_player_stats import LivePlayerStats
        from db.models.nba.games import Game

        # 1. ONE ESPN/Yahoo call for the whole week
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return WeeklyMatchupResp(status=matchup.status, message=matchup.message, data=None)

        md = matchup.data
        if not md.matchup_period_start or not md.matchup_period_end:
            return WeeklyMatchupResp(
                status=ApiStatus.NOT_FOUND,
                message="Matchup period dates unavailable — schedule may not cover current playoff period",
                data=None,
            )
        period_start = date_type.fromisoformat(md.matchup_period_start)
        period_end = date_type.fromisoformat(md.matchup_period_end)
        dates = [
            period_start + timedelta(days=i)
            for i in range((period_end - period_start).days + 1)
        ]

        today = _nba_today()
        scoring = resolve_scoring_for_team(team_id)
        all_roster = md.your_team.roster + md.opponent_team.roster
        resolve = make_nba_id_resolver(all_roster)
        all_nba_ids = list({nid for nid in (resolve(p) for p in all_roster) if nid is not None})

        # Bulk-fetch all DB data for the entire period in 3 queries
        past_and_today = [d for d in dates if d <= today]
        stats_by_player_date: dict[tuple[int, date_type], object] = {}
        if all_nba_ids and past_and_today:
            for st in PlayerGameStats.select().where(
                (PlayerGameStats.player_id.in_(all_nba_ids)) & (PlayerGameStats.game_date.in_(past_and_today))
            ):
                stats_by_player_date[(st.player_id, st.game_date)] = st

        live_by_player: dict[int, object] = {}
        if all_nba_ids and today in dates:
            for ls in LivePlayerStats.select().where(
                (LivePlayerStats.player_id.in_(all_nba_ids)) & (LivePlayerStats.game_date == today)
            ):
                live_by_player[ls.player_id] = ls

        games_by_date: dict[date_type, list] = {}
        for g in Game.select().where(Game.game_date.in_(dates)):
            games_by_date.setdefault(g.game_date, []).append(g)

        def stats_for_day(target_date: date_type) -> dict[int, object]:
            lookup: dict[int, object] = {}
            for nba_id in all_nba_ids:
                stat = stats_by_player_date.get((nba_id, target_date))
                if stat is None and target_date == today:
                    stat = live_by_player.get(nba_id)
                if stat is not None:
                    lookup[nba_id] = stat
            return lookup

        days = [
            build_day(md, d, today, period_start, stats_for_day(d), games_by_date.get(d, []), resolve, scoring)
            for d in dates
        ]

        return WeeklyMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Weekly matchup data fetched successfully",
            data=WeeklyMatchupData(matchup_period=md.matchup_period, days=days),
        )

    @staticmethod
    async def get_season_summary(team_id: int) -> SeasonSummaryResp:
        """
        Aggregate all DailyMatchupScore records for a team into a season summary.

        Uses the last day's snapshot per matchup period as the final score
        (the final score for that week after all games have been played).
        """
        records = list(
            DailyMatchupScore
            .select()
            .where(DailyMatchupScore.team_id == team_id)
            .order_by(DailyMatchupScore.matchup_period, DailyMatchupScore.date)
        )

        if not records:
            return SeasonSummaryResp(
                status=ApiStatus.NOT_FOUND,
                message=f"No season data found for team {team_id}",
                data=None,
            )

        # Group by matchup_period; last record per period = final score
        periods: dict[int, list] = {}
        for r in records:
            periods.setdefault(r.matchup_period, []).append(r)

        wins = losses = 0
        total_pf = total_pa = 0.0
        best_week: WeekResult | None = None
        worst_week: WeekResult | None = None
        weeks: list[WeekResult] = []

        any_categories = False
        for period in sorted(periods):
            last = periods[period][-1]
            pf = float(last.current_score)
            pa = float(last.opponent_current_score)
            won = pf > pa
            cs = last.category_scores if isinstance(last.category_scores, dict) else None
            if cs:
                any_categories = True

            wins += int(won)
            losses += int(not won)
            total_pf += pf
            total_pa += pa

            week = WeekResult(
                matchup_period=period,
                opponent_team_name=last.opponent_team_name,
                points_for=round(pf, 2),
                points_against=round(pa, 2),
                won=won,
                categories_won=int(cs.get("wins", pf)) if cs else None,
                categories_lost=int(cs.get("losses", pa)) if cs else None,
                categories_tied=int(cs.get("ties", 0)) if cs else None,
            )
            weeks.append(week)

            # Points leagues rank weeks by points scored; category leagues by net categories.
            margin = (pf - pa) if cs else pf
            best_margin = (best_week.points_for - best_week.points_against) if (best_week and cs) else (best_week.points_for if best_week else None)
            worst_margin = (worst_week.points_for - worst_week.points_against) if (worst_week and cs) else (worst_week.points_for if worst_week else None)
            if best_week is None or margin > best_margin:
                best_week = week
            if worst_week is None or margin < worst_margin:
                worst_week = week

        return SeasonSummaryResp(
            status=ApiStatus.SUCCESS,
            message="Season summary retrieved",
            data=SeasonSummaryData(
                team_id=team_id,
                team_name=records[0].team_name,
                wins=wins,
                losses=losses,
                total_points_for=round(total_pf, 2),
                total_points_against=round(total_pa, 2),
                best_week=best_week,
                worst_week=worst_week,
                weeks=weeks,
                scoring_format="categories" if any_categories else "points",
            ),
        )

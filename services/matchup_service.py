from datetime import datetime
from types import SimpleNamespace

from core.logging import get_logger
from core.settings import settings
from services.matchup_days import comparison_to_schema, nba_today as _nba_today, score_stat_row
from services.scoring.models import CategoryTeamScoreData, StatLine
from services.scoring.resolver import resolve_scoring_for_league_info, resolve_scoring_for_team
from schemas.matchup import CategoryTeamScore
from services.providers import get_provider_adapter
from services.matchup_history_service import MatchupHistoryService
# Compatibility exports for tests/scripts that patch provider clients at this module boundary.
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.team_service import TeamService
from schemas.matchup import (
    MatchupResp,
    LiveMatchupResp,
    LiveMatchupData,
    LiveMatchupTeam,
    LiveMatchupPlayer,
    PlayerLiveStats,
    DailyMatchupData,
    DailyMatchupTeam,
    DailyMatchupPlayerStats,
    DailyMatchupFuturePlayer,
)
from schemas.common import ApiStatus, LeagueInfo
from db.models.stats.daily_matchup_score import DailyMatchupScore
from db.base import run_db
from services.matchup_window import (
    DayWatermark, MatchupWindow, WatermarkSource, resolve_matchup_window,
)

log = get_logger("matchup")


class MatchupService(MatchupHistoryService):
    """Service for handling matchup-related operations."""

    @staticmethod
    def _baseline_snapshot(team_id: int, matchup_period: int):
        row = (
            DailyMatchupScore.select()
            .where(
                (DailyMatchupScore.team_id == team_id)
                & (DailyMatchupScore.matchup_period == matchup_period)
            )
            .order_by(DailyMatchupScore.date.desc())
            .first()
        )
        if row is None:
            return None
        return SimpleNamespace(
            date=row.date,
            current_score=row.current_score,
            opponent_current_score=row.opponent_current_score,
            category_scores=row.category_scores,
            scoring_period_id=row.scoring_period_id,
            scoring_period_source=row.scoring_period_source,
        )

    @staticmethod
    def _live_stat_snapshot(stat):
        return SimpleNamespace(
            player_id=stat.player_id,
            espn_id=stat.player.espn_id,
            name_normalized=stat.player.name_normalized,
            game_status=stat.game_status,
            game_clock=stat.game_clock,
            last_updated=stat.last_updated,
            period=stat.period,
            fpts=stat.fpts,
            pts=stat.pts,
            reb=stat.reb,
            ast=stat.ast,
            stl=stat.stl,
            blk=stat.blk,
            tov=stat.tov,
            min=stat.min,
            fgm=stat.fgm,
            fga=stat.fga,
            fg3m=stat.fg3m,
            fg3a=stat.fg3a,
            ftm=stat.ftm,
            fta=stat.fta,
        )

    @staticmethod
    def _live_stat_maps(espn_ids: list[int], names: list[str], overlay_date):
        from db.models.nba.live_player_stats import LivePlayerStats as LiveStatsModel

        by_espn = {
            stat.player.espn_id: MatchupService._live_stat_snapshot(stat)
            for stat in LiveStatsModel.get_live_stats_by_espn_ids(espn_ids, overlay_date)
        }
        unresolved = [name for name in names if name.lower().strip() not in {s.name_normalized for s in by_espn.values()}]
        by_name = {
            stat.player.name_normalized: MatchupService._live_stat_snapshot(stat)
            for stat in LiveStatsModel.get_live_stats_by_names(unresolved, overlay_date)
        } if unresolved else {}
        return by_espn, by_name

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
        scoring = await run_db(
            "matchups.resolve_scoring_for_league", resolve_scoring_for_league_info, league_info
        )
        return await get_provider_adapter(league_info.provider).get_matchup(
            league_info, avg_window, scoring=scoring
        )

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
        # Credentials, not the client-facing view: this goes straight to a
        # provider. Raises TEAM_NOT_FOUND for an unknown id.
        league_info = await TeamService.credentials_for(team_id)

        # Fetch matchup data using the league info - route by provider
        # Pass team_id for Yahoo so tokens can be refreshed and persisted
        scoring = await run_db("matchups.resolve_scoring", resolve_scoring_for_team, team_id)
        return await get_provider_adapter(league_info.provider).get_matchup(
            league_info, avg_window, team_id=team_id, scoring=scoring
        )

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
        # Step 1: Fetch the provider — always the default (current) scoring
        # period. We trust whatever it returns as the current roster and lineup.
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return LiveMatchupResp(status=matchup.status, message=matchup.message, data=None)

        espn_matchup_period = matchup.data.matchup_period

        # Step 2: Newest baseline snapshot for this matchup period. Ordering by
        # date still picks the newest row — watermarks are monotone, and
        # ordering by watermark would tie on days with no games.
        baseline = await run_db(
            "matchups.live_baseline", MatchupService._baseline_snapshot, team_id, espn_matchup_period
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
            all_names = [p.name for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster]
            espn_id_to_live, name_to_live = await run_db(
                "matchups.live_stats", MatchupService._live_stat_maps,
                all_espn_ids, all_names, overlay_date,
            )
        else:
            espn_id_to_live = {}
            name_to_live = {}

        scoring = await run_db("matchups.resolve_live_scoring", resolve_scoring_for_team, team_id)
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

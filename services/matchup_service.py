from datetime import datetime, date as date_type, timedelta

from services.espn_service import EspnService
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
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window)
        return await EspnService.get_matchup_data(league_info, avg_window)

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
            MatchupResp with matchup data or error
        """
        # Get the team's league info
        team_resp = await TeamService.view_team(team_id)

        if team_resp.status != ApiStatus.SUCCESS or not team_resp.data:
            return MatchupResp(
                status=ApiStatus.NOT_FOUND,
                message=f"Team with ID {team_id} not found",
                data=None
            )

        league_info = team_resp.data.league_info

        # Fetch matchup data using the league info - route by provider
        # Pass team_id for Yahoo so tokens can be refreshed and persisted
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.get_matchup_data(league_info, avg_window, team_id)
        return await EspnService.get_matchup_data(league_info, avg_window)

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
        import pytz
        from db.models.nba.live_player_stats import LivePlayerStats as LiveStatsModel

        eastern = pytz.timezone("US/Eastern")
        now_et = datetime.now(eastern)

        # NBA date convention: before 6 AM ET counts as yesterday.
        # This matches how the live pipeline stores records and ensures
        # we don't prematurely advance to the next day after midnight.
        nba_today = (now_et - timedelta(days=1)).date() if now_et.hour < 6 else now_et.date()

        # Step 1: Fetch ESPN — always use the default (current) scoring period.
        # We trust whatever ESPN returns as the current roster and lineup.
        matchup = await MatchupService.get_matchup_by_team_id(user_id, team_id, avg_window="season")

        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            return LiveMatchupResp(status=matchup.status, message=matchup.message, data=None)

        espn_matchup_period = matchup.data.matchup_period
        espn_scoring_period = matchup.data.scoring_period_id

        # Step 2: Query the latest DailyMatchupScore baseline for this matchup period.
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

        # Step 3: Determine base scores and live overlay date.
        #
        # Timeline: ESPN batches at ~2 AM ET; our pipeline runs at ~2:30 AM ET (7:30 AM UTC).
        # baseline.date = D means captured at 4 AM on D, after ESPN's batch,
        # so it already reflects cumulative scores through end of day D-1.
        # Games on day D are never in baseline.date = D.
        #
        # Live overlay is needed when nba_today >= baseline.date:
        #   - baseline.date == nba_today: common case, today's games not captured yet.
        #   - baseline.date < nba_today: baseline is stale (pipeline hasn't run today).
        #   - baseline.date > nba_today: pipeline already ran for tomorrow
        #     (4 AM ET window when nba_today=yesterday), baseline already has today → no overlay.
        #
        # Example timeline (day N = Friday, games end ~midnight):
        #   Fri 8 PM:  nba_today=Fri, baseline.date=Fri  → overlay Fri live ✓
        #   Sat 1 AM:  nba_today=Fri, baseline.date=Fri  → overlay Fri live ✓
        #   Sat 2 AM:  ESPN batch runs (totalPoints updated, lineup flipped)
        #   Sat 2:15 AM: nba_today=Fri, baseline.date=Fri → overlay Fri live ✓
        #   Sat 2:30 AM: pipeline runs → baseline.date=Sat (includes Fri games)
        #   Sat 2:45 AM: nba_today=Fri, baseline.date=Sat → Sat > Fri → no overlay, game_date=Sat ✓
        #   Sat 6 AM:  nba_today=Sat, baseline.date=Sat  → overlay Sat live (0 until tipoff) ✓
        # When the pipeline has already run for the next calendar day
        # (baseline.date > nba_today), advance game_date so the frontend shows
        # tomorrow's upcoming view instead of staying stuck on yesterday.
        # This happens in the ~30 min window after ESPN batch + pipeline run,
        # before the 6 AM ET cutoff advances nba_today naturally.
        if baseline and baseline.date > nba_today:
            game_date = baseline.date
        else:
            game_date = nba_today
        if baseline:
            your_base = float(baseline.current_score)
            opponent_base = float(baseline.opponent_current_score)
            # baseline.date = D means captured at ~4 AM ET on day D (after ESPN's
            # 3 AM batch), so it reflects cumulative scores through end of day D-1.
            # Games on day D are NOT in the baseline.
            # We need live overlay whenever nba_today >= baseline.date, i.e. when
            # today's games couldn't be in the baseline yet.
            # Using <= (not <) because baseline.date == nba_today is the common case:
            # the baseline from 4 AM today doesn't include today's games.
            # Edge: if pipeline already ran for nba_today+1 (baseline.date > nba_today),
            # the baseline already includes nba_today's games — no overlay needed.
            baseline_excludes_nba_today = baseline.date <= nba_today
        else:
            your_base = matchup.data.your_team.current_score
            opponent_base = matchup.data.opponent_team.current_score
            baseline_excludes_nba_today = False

        # Include live overlay only when nba_today's games aren't yet in the
        # baseline, AND game_date falls within the current ESPN matchup period.
        # Use the matchup's actual date range (already correctly spanning 2 weeks
        # for playoff periods) rather than comparing local-schedule week numbers,
        # which diverge from ESPN's matchup period IDs during playoffs.
        include_live = False
        if (
            baseline_excludes_nba_today
            and matchup.data.matchup_period_start
            and matchup.data.matchup_period_end
        ):
            mp_start = date_type.fromisoformat(matchup.data.matchup_period_start)
            mp_end = date_type.fromisoformat(matchup.data.matchup_period_end)
            include_live = mp_start <= game_date <= mp_end

        if include_live:
            all_espn_ids = [
                p.player_id for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster
            ]
            live_stats_list = LiveStatsModel.get_live_stats_by_espn_ids(all_espn_ids, game_date)
            espn_id_to_live = {stat.player.espn_id: stat for stat in live_stats_list}

            # Name-based fallback for players without espn_id mapping
            unresolved_names = [
                p.name for p in matchup.data.your_team.roster + matchup.data.opponent_team.roster
                if p.player_id not in espn_id_to_live
            ]
            name_to_live: dict[str, object] = {}
            if unresolved_names:
                fallback_stats = LiveStatsModel.get_live_stats_by_names(unresolved_names, game_date)
                name_to_live = {stat.player.name_normalized: stat for stat in fallback_stats}
        else:
            espn_id_to_live = {}
            name_to_live = {}

        def build_live_roster(roster) -> list[LiveMatchupPlayer]:
            result = []
            for p in roster:
                stat = espn_id_to_live.get(p.player_id) or name_to_live.get(p.name.lower().strip())
                live_overlay = None
                if stat:
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
                        live_fpts=stat.fpts,
                        live_pts=stat.pts,
                        live_reb=stat.reb,
                        live_ast=stat.ast,
                        live_stl=stat.stl,
                        live_blk=stat.blk,
                        live_tov=stat.tov,
                        live_min=stat.min,
                        game_status=effective_status,
                        period=stat.period if effective_status == 2 else None,
                        game_clock=effective_clock,
                        last_updated=stat.last_updated.isoformat() if stat.last_updated else None,
                    )
                result.append(LiveMatchupPlayer(**p.model_dump(), live=live_overlay))
            return result

        def compute_live_score(base: float, live_roster: list[LiveMatchupPlayer]) -> float:
            """
            Add today's live fpts for active roster players (not BE/IR) on
            top of the pipeline baseline score. Both in-progress (status 2)
            and final (status 3) games are included because the baseline is
            a morning snapshot captured before any games start — tonight's
            finished games are NOT yet reflected in it. The live pipeline
            cleans up stale records from previous game days on each run, so
            all records in live_player_stats are from today only.
            With name_to_live={}, today_fpts=0 and base is returned unchanged.
            """
            today_fpts = sum(
                p.live.live_fpts
                for p in live_roster
                if p.lineup_slot not in ("BE", "IR")
                and p.live is not None
                and p.live.game_status >= 2
            )
            return round(base + today_fpts, 2)

        your_live_roster = build_live_roster(matchup.data.your_team.roster)
        opponent_live_roster = build_live_roster(matchup.data.opponent_team.roster)

        your_team = LiveMatchupTeam(
            team_name=matchup.data.your_team.team_name,
            team_id=matchup.data.your_team.team_id,
            current_score=compute_live_score(your_base, your_live_roster),
            projected_score=matchup.data.your_team.projected_score,
            roster=your_live_roster,
        )
        opponent_team = LiveMatchupTeam(
            team_name=matchup.data.opponent_team.team_name,
            team_id=matchup.data.opponent_team.team_id,
            current_score=compute_live_score(opponent_base, opponent_live_roster),
            projected_score=matchup.data.opponent_team.projected_score,
            roster=opponent_live_roster,
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
        """
        try:
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
                    return MatchupScoreHistoryResp(
                        status=ApiStatus.NOT_FOUND,
                        message="No score history found for this team",
                        data=None
                    )
                query = query.where(DailyMatchupScore.matchup_period == latest.matchup_period)

            records = list(query.order_by(DailyMatchupScore.day_of_matchup.asc()))

            if not records:
                return MatchupScoreHistoryResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No score history found for this matchup period",
                    data=None
                )

            first_record = records[0]
            history = [
                DailyScorePoint(
                    date=record.date.isoformat(),
                    day_of_matchup=record.day_of_matchup,
                    your_score=float(record.current_score),
                    opponent_score=float(record.opponent_current_score)
                )
                for record in records
            ]

            return MatchupScoreHistoryResp(
                status=ApiStatus.SUCCESS,
                message="Score history retrieved successfully",
                data=MatchupScoreHistory(
                    team_id=team_id,
                    team_name=first_record.team_name,
                    opponent_team_name=first_record.opponent_team_name,
                    matchup_period=first_record.matchup_period,
                    history=history
                )
            )

        except Exception as e:
            return MatchupScoreHistoryResp(
                status=ApiStatus.ERROR,
                message=f"Failed to fetch score history: {str(e)}",
                data=None
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

        # 3. Determine day_type using the NBA date convention:
        #    before 6 AM ET counts as yesterday (games that started the
        #    previous evening). This matches how the live pipeline stores
        #    records and prevents the day from advancing prematurely after
        #    midnight when ESPN has flipped but games may still be in progress.
        eastern = pytz.timezone("US/Eastern")
        now_et = datetime.now(eastern)
        today = (now_et - timedelta(days=1)).date() if now_et.hour < 6 else now_et.date()

        if target_date < today:
            day_type = "past"
        elif target_date == today:
            day_type = "today"
        else:
            day_type = "future"

        day_index = (target_date - period_start).days

        # 4. Resolve roster players → NBA player IDs via espn_id (primary)
        #    with name-based fallback for any players missing espn_id mapping.
        all_roster = md.your_team.roster + md.opponent_team.roster
        espn_ids = [p.player_id for p in all_roster]
        players_by_espn = list(Player.select().where(
            Player.espn_id.in_(espn_ids)
        ))
        espn_to_nba = {p.espn_id: p.id for p in players_by_espn}

        # Name-based fallback for players not resolved via espn_id
        unresolved_names = [
            p.name.lower().strip() for p in all_roster
            if p.player_id not in espn_to_nba
        ]
        name_to_nba: dict[str, int] = {}
        if unresolved_names:
            players_by_name = list(Player.select().where(
                Player.name_normalized.in_(unresolved_names)
            ))
            name_to_nba = {p.name_normalized: p.id for p in players_by_name}

        def resolve_nba_id(roster_player) -> int | None:
            """Resolve a roster player to an NBA player ID. ESPN ID first, then name."""
            nba_id = espn_to_nba.get(roster_player.player_id)
            if nba_id:
                return nba_id
            return name_to_nba.get(roster_player.name.lower().strip())

        # 5. Get games on the target date
        games_on_date = Game.get_games_on_date(target_date)
        teams_playing = set()
        # Build team → game mapping for opponent info
        team_game_map: dict[str, Game] = {}
        for game in games_on_date:
            teams_playing.add(game.home_team_id)
            teams_playing.add(game.away_team_id)
            team_game_map[game.home_team_id] = game
            team_game_map[game.away_team_id] = game

        if day_type in ("past", "today"):
            # 6a. Past/today: look up player stats
            nba_ids = [resolve_nba_id(p) for p in all_roster]
            nba_ids = [nid for nid in nba_ids if nid is not None]
            stats_list = list(
                PlayerGameStats.select()
                .where(
                    (PlayerGameStats.player_id.in_(nba_ids))
                    & (PlayerGameStats.game_date == target_date)
                )
            ) if nba_ids else []
            nba_id_to_stats = {s.player_id: s for s in stats_list}

            # For today: overlay live stats for players not yet in PlayerGameStats.
            # The nightly pipeline hasn't run yet, so PlayerGameStats is empty for
            # today — pull from the live_player_stats table instead (same source
            # the live matchup endpoint uses).
            if day_type == "today" and nba_ids:
                from db.models.nba.live_player_stats import LivePlayerStats
                live_stats_list = list(
                    LivePlayerStats.select()
                    .where(
                        (LivePlayerStats.player_id.in_(nba_ids))
                        & (LivePlayerStats.game_date == target_date)
                    )
                )
                for ls in live_stats_list:
                    if ls.player_id not in nba_id_to_stats:
                        nba_id_to_stats[ls.player_id] = ls

            def build_past_roster(roster) -> list[DailyMatchupPlayerStats]:
                result = []
                for p in roster:
                    nba_id = resolve_nba_id(p)
                    had_game = p.team in teams_playing
                    stats = nba_id_to_stats.get(nba_id) if nba_id else None
                    result.append(DailyMatchupPlayerStats(
                        player_id=p.player_id,
                        name=p.name,
                        team=p.team,
                        position=p.position,
                        nba_player_id=nba_id,
                        had_game=had_game,
                        fpts=stats.fpts if stats else None,
                        pts=stats.pts if stats else None,
                        reb=stats.reb if stats else None,
                        ast=stats.ast if stats else None,
                        stl=stats.stl if stats else None,
                        blk=stats.blk if stats else None,
                        tov=stats.tov if stats else None,
                        min=stats.min if stats else None,
                        fgm=stats.fgm if stats else None,
                        fga=stats.fga if stats else None,
                        fg3m=stats.fg3m if stats else None,
                        fg3a=stats.fg3a if stats else None,
                        ftm=stats.ftm if stats else None,
                        fta=stats.fta if stats else None,
                    ))
                # Sort: players with stats first (by fpts desc), then had_game but no stats, then no game
                result.sort(key=lambda x: (
                    0 if x.fpts is not None else (1 if x.had_game else 2),
                    -(x.fpts or 0),
                ))
                return result

            your_roster = build_past_roster(md.your_team.roster)
            opp_roster = build_past_roster(md.opponent_team.roster)

            your_total = sum(p.fpts for p in your_roster if p.fpts is not None)
            opp_total = sum(p.fpts for p in opp_roster if p.fpts is not None)

            your_team = DailyMatchupTeam(
                team_name=md.your_team.team_name,
                team_id=md.your_team.team_id,
                total_fpts=float(your_total),
                roster=your_roster,
            )
            opponent_team = DailyMatchupTeam(
                team_name=md.opponent_team.team_name,
                team_id=md.opponent_team.team_id,
                total_fpts=float(opp_total),
                roster=opp_roster,
            )

        else:
            # 6b. Future: show which players have games
            def build_future_roster(roster) -> list[DailyMatchupFuturePlayer]:
                result = []
                for p in roster:
                    game = team_game_map.get(p.team)
                    has_game = game is not None
                    opponent = None
                    game_time = None
                    if game:
                        if game.home_team_id == p.team:
                            opponent = f"vs {game.away_team_id}"
                        else:
                            opponent = f"@ {game.home_team_id}"
                        game_time = str(game.start_time_et) if game.start_time_et else None
                    result.append(DailyMatchupFuturePlayer(
                        player_id=p.player_id,
                        name=p.name,
                        team=p.team,
                        position=p.position,
                        has_game=has_game,
                        opponent=opponent,
                        game_time_et=game_time,
                        injured=p.injured,
                        injury_status=p.injury_status,
                    ))
                # Players with games first, then without
                result.sort(key=lambda x: (0 if x.has_game else 1, x.name))
                return result

            your_roster = build_future_roster(md.your_team.roster)
            opp_roster = build_future_roster(md.opponent_team.roster)

            your_team = DailyMatchupTeam(
                team_name=md.your_team.team_name,
                team_id=md.your_team.team_id,
                total_fpts=None,
                roster=your_roster,
            )
            opponent_team = DailyMatchupTeam(
                team_name=md.opponent_team.team_name,
                team_id=md.opponent_team.team_id,
                total_fpts=None,
                roster=opp_roster,
            )

        return DailyMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Daily matchup data fetched successfully",
            data=DailyMatchupData(
                date=target_date.isoformat(),
                day_type=day_type,
                day_of_week=target_date.strftime("%a"),
                day_index=day_index,
                matchup_period=md.matchup_period,
                matchup_period_start=md.matchup_period_start,
                matchup_period_end=md.matchup_period_end,
                your_team=your_team,
                opponent_team=opponent_team,
            ),
        )

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

        # 2. NBA date convention: before 6 AM ET counts as yesterday
        eastern = pytz.timezone("US/Eastern")
        now_et = datetime.now(eastern)
        nba_today = (now_et - timedelta(days=1)).date() if now_et.hour < 6 else now_et.date()

        # 3. Resolve all roster players → NBA IDs once for the whole week
        all_roster = md.your_team.roster + md.opponent_team.roster
        espn_ids = [p.player_id for p in all_roster]
        players_by_espn = list(Player.select().where(Player.espn_id.in_(espn_ids)))
        espn_to_nba = {p.espn_id: p.id for p in players_by_espn}

        unresolved_names = [
            p.name.lower().strip() for p in all_roster
            if p.player_id not in espn_to_nba
        ]
        name_to_nba: dict[str, int] = {}
        if unresolved_names:
            players_by_name = list(Player.select().where(
                Player.name_normalized.in_(unresolved_names)
            ))
            name_to_nba = {p.name_normalized: p.id for p in players_by_name}

        def resolve_nba_id(roster_player) -> int | None:
            nba_id = espn_to_nba.get(roster_player.player_id)
            if nba_id:
                return nba_id
            return name_to_nba.get(roster_player.name.lower().strip())

        all_nba_ids = list({
            nid for p in all_roster
            if (nid := resolve_nba_id(p)) is not None
        })

        # 4. Bulk-fetch all DB data for the entire period in 3 queries
        past_and_today = [d for d in dates if d <= nba_today]

        # PlayerGameStats for all past/today dates at once
        stats_by_player_date: dict[tuple[int, date_type], object] = {}
        if all_nba_ids and past_and_today:
            stats_rows = list(
                PlayerGameStats.select()
                .where(
                    (PlayerGameStats.player_id.in_(all_nba_ids))
                    & (PlayerGameStats.game_date.in_(past_and_today))
                )
            )
            for s in stats_rows:
                stats_by_player_date[(s.player_id, s.game_date)] = s

        # LivePlayerStats for today only (overlays missing PlayerGameStats)
        live_by_player: dict[int, object] = {}
        if all_nba_ids and nba_today in dates:
            live_rows = list(
                LivePlayerStats.select()
                .where(
                    (LivePlayerStats.player_id.in_(all_nba_ids))
                    & (LivePlayerStats.game_date == nba_today)
                )
            )
            for ls in live_rows:
                live_by_player[ls.player_id] = ls

        # All games for the entire matchup period at once
        all_games = list(Game.select().where(Game.game_date.in_(dates)))
        games_by_date: dict[date_type, list] = {}
        for g in all_games:
            games_by_date.setdefault(g.game_date, []).append(g)

        # 5. Build per-day data using pre-fetched lookups
        def build_day(target_date: date_type) -> DailyMatchupData:
            if target_date < nba_today:
                day_type = "past"
            elif target_date == nba_today:
                day_type = "today"
            else:
                day_type = "future"

            day_index = (target_date - period_start).days

            games_on_date = games_by_date.get(target_date, [])
            teams_playing: set[str] = set()
            team_game_map: dict[str, object] = {}
            for game in games_on_date:
                teams_playing.add(game.home_team_id)
                teams_playing.add(game.away_team_id)
                team_game_map[game.home_team_id] = game
                team_game_map[game.away_team_id] = game

            if day_type in ("past", "today"):
                # Merge PlayerGameStats + live overlay into a single lookup
                nba_id_to_stats: dict[int, object] = {}
                for p in all_roster:
                    nba_id = resolve_nba_id(p)
                    if nba_id is None:
                        continue
                    stat = stats_by_player_date.get((nba_id, target_date))
                    if stat:
                        nba_id_to_stats[nba_id] = stat
                    elif day_type == "today":
                        live = live_by_player.get(nba_id)
                        if live:
                            nba_id_to_stats[nba_id] = live

                def build_past_roster(roster) -> list[DailyMatchupPlayerStats]:
                    result = []
                    for p in roster:
                        nba_id = resolve_nba_id(p)
                        had_game = p.team in teams_playing
                        stats = nba_id_to_stats.get(nba_id) if nba_id else None
                        result.append(DailyMatchupPlayerStats(
                            player_id=p.player_id,
                            name=p.name,
                            team=p.team,
                            position=p.position,
                            nba_player_id=nba_id,
                            had_game=had_game,
                            fpts=stats.fpts if stats else None,
                            pts=stats.pts if stats else None,
                            reb=stats.reb if stats else None,
                            ast=stats.ast if stats else None,
                            stl=stats.stl if stats else None,
                            blk=stats.blk if stats else None,
                            tov=stats.tov if stats else None,
                            min=stats.min if stats else None,
                            fgm=stats.fgm if stats else None,
                            fga=stats.fga if stats else None,
                            fg3m=stats.fg3m if stats else None,
                            fg3a=stats.fg3a if stats else None,
                            ftm=stats.ftm if stats else None,
                            fta=stats.fta if stats else None,
                        ))
                    result.sort(key=lambda x: (
                        0 if x.fpts is not None else (1 if x.had_game else 2),
                        -(x.fpts or 0),
                    ))
                    return result

                your_roster = build_past_roster(md.your_team.roster)
                opp_roster = build_past_roster(md.opponent_team.roster)
                your_total = sum(p.fpts for p in your_roster if p.fpts is not None)
                opp_total = sum(p.fpts for p in opp_roster if p.fpts is not None)

                your_team = DailyMatchupTeam(
                    team_name=md.your_team.team_name,
                    team_id=md.your_team.team_id,
                    total_fpts=float(your_total),
                    roster=your_roster,
                )
                opponent_team = DailyMatchupTeam(
                    team_name=md.opponent_team.team_name,
                    team_id=md.opponent_team.team_id,
                    total_fpts=float(opp_total),
                    roster=opp_roster,
                )

            else:
                def build_future_roster(roster) -> list[DailyMatchupFuturePlayer]:
                    result = []
                    for p in roster:
                        game = team_game_map.get(p.team)
                        has_game = game is not None
                        opponent = None
                        game_time = None
                        if game:
                            if game.home_team_id == p.team:
                                opponent = f"vs {game.away_team_id}"
                            else:
                                opponent = f"@ {game.home_team_id}"
                            game_time = str(game.start_time_et) if game.start_time_et else None
                        result.append(DailyMatchupFuturePlayer(
                            player_id=p.player_id,
                            name=p.name,
                            team=p.team,
                            position=p.position,
                            has_game=has_game,
                            opponent=opponent,
                            game_time_et=game_time,
                            injured=p.injured,
                            injury_status=p.injury_status,
                        ))
                    result.sort(key=lambda x: (0 if x.has_game else 1, x.name))
                    return result

                your_roster = build_future_roster(md.your_team.roster)
                opp_roster = build_future_roster(md.opponent_team.roster)
                your_team = DailyMatchupTeam(
                    team_name=md.your_team.team_name,
                    team_id=md.your_team.team_id,
                    total_fpts=None,
                    roster=your_roster,
                )
                opponent_team = DailyMatchupTeam(
                    team_name=md.opponent_team.team_name,
                    team_id=md.opponent_team.team_id,
                    total_fpts=None,
                    roster=opp_roster,
                )

            return DailyMatchupData(
                date=target_date.isoformat(),
                day_type=day_type,
                day_of_week=target_date.strftime("%a"),
                day_index=day_index,
                matchup_period=md.matchup_period,
                matchup_period_start=md.matchup_period_start,
                matchup_period_end=md.matchup_period_end,
                your_team=your_team,
                opponent_team=opponent_team,
            )

        days = [build_day(d) for d in dates]

        return WeeklyMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Weekly matchup data fetched successfully",
            data=WeeklyMatchupData(
                matchup_period=md.matchup_period,
                days=days,
            ),
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

        for period in sorted(periods):
            last = periods[period][-1]
            pf = float(last.current_score)
            pa = float(last.opponent_current_score)
            won = pf > pa

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
            )
            weeks.append(week)

            if best_week is None or pf > best_week.points_for:
                best_week = week
            if worst_week is None or pf < worst_week.points_for:
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
            ),
        )

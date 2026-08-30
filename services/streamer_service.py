from typing import Optional
from datetime import date, timedelta

from schemas.streamer import (
    StreamerResp,
    StreamerData,
    StreamerPlayerResp,
    StreamerMode
)
from core.errors import BadRequestError
from schemas.common import ApiStatus, LeagueInfo, FantasyProvider
from services.providers import get_provider_adapter
# Compatibility exports for callers that patch provider clients here.
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.player_service import PlayerService, _normalize_name
from services.player_value_service import PlayerValueService
from db.models.nba.players import Player as PlayerModel
from db.base import run_db
from services.schedule_service import (
    get_current_matchup,
    get_nba_today,
    get_remaining_games,
    get_remaining_game_days,
    has_remaining_b2b,
    get_b2b_game_count,
    get_teams_with_b2b
)


class StreamerService:
    """Service for finding and ranking streaming candidates."""

    # Week mode: values schedule density (more games = more value)
    WEEK_WEIGHTS = {
        "b2b": 50.0,
        "games_remaining": 10.0,
        "avg_points": 1.0,
        "b2b_games": 5.0,
    }

    # Daily mode: values per-game performance, B2B still meaningful (2 games for 1 pickup)
    DAILY_WEIGHTS = {
        "b2b": 15.0,
        "games_remaining": 2.0,
        "avg_points": 3.0,
        "b2b_games": 8.0,
    }

    @staticmethod
    def _stored_values(league_info, team_id, free_agents, avg_days, is_yahoo):
        scoring = PlayerValueService.scoring_for(league_info, team_id)
        value_kind = PlayerValueService.value_kind_for(scoring)
        if is_yahoo:
            lookups = [(player.name, player.team) for player in free_agents]
            values = PlayerValueService.avg_points_for(scoring, names=lookups, days=avg_days)
        else:
            values = PlayerValueService.avg_points_for(
                scoring, espn_ids=[player.player_id for player in free_agents], days=avg_days
            )
        return value_kind, values

    @staticmethod
    def _nba_ids(espn_ids: list[int]) -> dict[int, int]:
        return {
            row.espn_id: row.id
            for row in PlayerModel.select(PlayerModel.id, PlayerModel.espn_id)
            .where(PlayerModel.espn_id.in_(espn_ids))
        }

    @staticmethod
    def _get_daily_b2b_metrics(game_days: list[int], pickup_day: int) -> tuple[bool, int]:
        """
        In daily mode, only count B2Bs that include the pickup day itself.

        Example:
        - pickup_day=0 with games [0, 3, 4] => no daily-relevant B2B
        - pickup_day=0 with games [0, 1, 4] => daily-relevant B2B
        """
        has_pickup_day_b2b = pickup_day in game_days and (pickup_day + 1) in game_days
        return has_pickup_day_b2b, (2 if has_pickup_day_b2b else 0)

    @staticmethod
    def _calculate_streamer_score(
        has_b2b: bool,
        games_remaining: int,
        avg_points_last_n: Optional[float],
        b2b_game_count: int,
        weights: dict
    ) -> float:
        """
        Calculate the streaming score for a player.

        Score components are weighted differently based on mode:
        - Week mode: favors schedule density (games remaining, B2B sequences)
        - Daily mode: favors per-game performance, with B2B as a meaningful bonus

        Returns:
            The calculated streamer score.
        """
        score = 0.0

        if has_b2b:
            score += weights["b2b"]

        score += games_remaining * weights["games_remaining"]

        if avg_points_last_n is not None:
            score += avg_points_last_n * weights["avg_points"]

        score += b2b_game_count * weights["b2b_games"]

        return round(score, 1)

    @staticmethod
    async def find_streamers(
        league_info: LeagueInfo,
        fa_count: int = 300,
        exclude_injured: bool = True,
        b2b_only: bool = False,
        mode: StreamerMode = StreamerMode.WEEK,
        target_day: Optional[int] = None,
        avg_days: int = 7,
        team_id: Optional[int] = None
    ) -> StreamerResp:
        """
        Find and rank the best streaming candidates from free agents.

        Supports two modes:
        - week: Rank by rest-of-week value (schedule density + performance).
        - daily: Rank by single-day pickup value (performance-focused).
                 Only returns players with a game on the target day.
                 B2B only applies when target day and next day are both games.

        Args:
            league_info: ESPN/Yahoo league credentials and team info.
            fa_count: Number of free agents to fetch (default 50).
            exclude_injured: Whether to exclude injured players (default True).
            b2b_only: Only show B2B players (week: any remaining B2B, daily: target day + next day).
            mode: Scoring mode - 'week' or 'daily'.
            target_day: Day index for daily mode (0-indexed). If None, uses current day.
            avg_days: Number of days for rolling average calculation (default 7).

        Returns:
            StreamerResp with ranked list of streaming candidates; SUCCESS with data=None
            when no matchup is active today.

        Raises:
            BadRequestError (TARGET_DAY_OUT_OF_RANGE / LEAGUE_VALIDATION_FAILED) and the
            provider services' typed AppErrors.
        """
        # Get current matchup info (using today to determine which matchup we're in)
        matchup = get_current_matchup()
        if not matchup:
            # An empty state, not a failure (offseason / All-Star break): 200 with data: null
            return StreamerResp(
                status=ApiStatus.SUCCESS,
                message="No active matchup for today — streaming picks return with the next matchup week",
                data=None
            )

        matchup_number = matchup["matchup_number"]
        game_span = matchup["game_span"]
        start_date = matchup["start_date"]
        current_day_index = matchup["current_day_index"]

        # Determine effective date based on mode
        if mode == StreamerMode.DAILY and target_day is not None:
            # Validate target_day is within matchup bounds
            if target_day >= game_span:
                raise BadRequestError(
                    "TARGET_DAY_OUT_OF_RANGE",
                    f"Day {target_day} is out of bounds. Matchup has {game_span} days (0-{game_span - 1}).",
                )
            effective_date = start_date + timedelta(days=target_day)
        else:
            if mode == StreamerMode.DAILY:
                # Default to current day; derive effective_date from start_date+index
                # so game_days filtering is consistent with the explicit targetDay path.
                # Using date.today() (UTC) would drift from _get_nba_today() (ET) after
                # ~7 PM ET (midnight UTC), causing target_day to fall outside game_days.
                target_day = current_day_index
                effective_date = start_date + timedelta(days=target_day)
            else:
                # ET fantasy day, consistent with get_current_matchup() above
                # (date.today() is UTC on Railway and drifts after ~7 PM ET).
                effective_date = get_nba_today()

        # Select scoring weights based on mode (distinct from the league's
        # point weights below — they were once the same variable, which
        # made every request 500 with KeyError('games_remaining')).
        score_weights = (
            StreamerService.DAILY_WEIGHTS
            if mode == StreamerMode.DAILY
            else StreamerService.WEEK_WEIGHTS
        )

        # Get teams with B2B games.
        # Daily mode only considers B2Bs that include the pickup day itself.
        if mode == StreamerMode.DAILY:
            teams_with_b2b = sorted(
                team
                for team, team_games in matchup["games"].items()
                if str(target_day) in team_games and str(target_day + 1) in team_games
            )
        else:
            teams_with_b2b = get_teams_with_b2b(effective_date)

        adapter = get_provider_adapter(league_info.provider)
        is_yahoo = adapter.uses_name_identity
        fa_response = await adapter.get_free_agents(league_info, fa_count, team_id=team_id)

        # Provider failures raise (403/400/502/504) before we get here; a non-success envelope is a
        # league configuration problem, and an empty free-agent pool is simply zero candidates.
        if fa_response.status != ApiStatus.SUCCESS:
            raise BadRequestError(fa_response.error_code or "LEAGUE_VALIDATION_FAILED",
                                  fa_response.message or "Could not fetch free agents")

        free_agents = fa_response.data or []

        # Value free agents from our stored stats under this league's scoring:
        # fantasy points under its weights, or the category value for H2H-category
        # leagues (rolling window, then last season's baseline).
        # Yahoo uses name-based lookup, ESPN uses player ID.
        value_kind, last_n_values = await run_db(
            "streamers.values", StreamerService._stored_values,
            league_info, team_id, free_agents, avg_days, is_yahoo,
        )

        # Build streamer list
        streamers: list[StreamerPlayerResp] = []

        for fa in free_agents:
            # Skip injured players if requested
            if exclude_injured and fa.injured:
                continue

            # Get team schedule info
            team = fa.team
            game_days = get_remaining_game_days(team, effective_date)
            games_remaining = get_remaining_games(team, effective_date)

            # Skip players with no remaining games
            if games_remaining == 0:
                continue

            # In daily mode, only include players with a game on the target day
            if mode == StreamerMode.DAILY and target_day not in game_days:
                continue

            if mode == StreamerMode.DAILY:
                team_has_b2b, b2b_game_count = StreamerService._get_daily_b2b_metrics(
                    game_days=game_days,
                    pickup_day=target_day
                )
            else:
                team_has_b2b = has_remaining_b2b(team, effective_date)
                b2b_game_count = get_b2b_game_count(team, effective_date)

            # Skip non-B2B teams if b2b_only is set
            if b2b_only and not team_has_b2b:
                continue

            # Our value for the player (Yahoo values are keyed by the diacritic-stripped name)
            valued = last_n_values.get(_normalize_name(fa.name) if is_yahoo else fa.player_id)
            avg_points_last_n = valued.value if valued is not None else None
            avg_source = valued.source if valued is not None else None

            # Calculate streamer score
            streamer_score = StreamerService._calculate_streamer_score(
                has_b2b=team_has_b2b,
                games_remaining=games_remaining,
                avg_points_last_n=avg_points_last_n,
                b2b_game_count=b2b_game_count,
                weights=score_weights
            )

            streamers.append(StreamerPlayerResp(
                player_id=fa.player_id,
                name=fa.name,
                team=team,
                valid_positions=fa.valid_positions,
                avg_points_last_n=avg_points_last_n,
                avg_points_season=fa.avg_points,
                avg_source=avg_source,
                games_remaining=games_remaining,
                has_b2b=team_has_b2b,
                b2b_game_count=b2b_game_count,
                game_days=game_days,
                streamer_score=streamer_score,
                injured=fa.injured,
                injury_status=None  # Could be enhanced later
            ))

        # Batch-resolve ESPN IDs → NBA player IDs for terminal navigation
        espn_ids = [s.player_id for s in streamers]
        if espn_ids:
            nba_id_map = await run_db("streamers.nba_ids", StreamerService._nba_ids, espn_ids)
            for s in streamers:
                s.nba_player_id = nba_id_map.get(s.player_id)

        # Week mode: group B2B first, then by score. Daily mode: purely by score.
        if mode == StreamerMode.WEEK:
            streamers.sort(key=lambda x: (-x.has_b2b, -x.streamer_score))
        else:
            streamers.sort(key=lambda x: -x.streamer_score)

        return StreamerResp(
            status=ApiStatus.SUCCESS,
            message=f"Found {len(streamers)} streaming candidates",
            data=StreamerData(
                matchup_number=matchup_number,
                current_day_index=current_day_index,
                game_span=game_span,
                avg_days=avg_days,
                mode=mode,
                target_day=target_day if mode == StreamerMode.DAILY else None,
                teams_with_b2b=teams_with_b2b,
                streamers=streamers,
                value_kind=value_kind,
            )
        )

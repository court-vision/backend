from typing import Optional
from datetime import date, timedelta

from schemas.streamer import (
    StreamerResp,
    StreamerData,
    StreamerPlayerResp,
    StreamerMode
)
from schemas.common import ApiStatus, LeagueInfo, FantasyProvider
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.player_service import PlayerService
from services.schedule_service import (
    get_current_matchup,
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
        fa_count: int = 50,
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

        Args:
            league_info: ESPN/Yahoo league credentials and team info.
            fa_count: Number of free agents to fetch (default 50).
            exclude_injured: Whether to exclude injured players (default True).
            b2b_only: Only show players on teams with remaining B2Bs (default False).
            mode: Scoring mode - 'week' or 'daily'.
            target_day: Day index for daily mode (0-indexed). If None, uses current day.
            avg_days: Number of days for rolling average calculation (default 7).

        Returns:
            StreamerResp with ranked list of streaming candidates.
        """
        try:
            # Get current matchup info (using today to determine which matchup we're in)
            matchup = get_current_matchup()
            if not matchup:
                return StreamerResp(
                    status=ApiStatus.ERROR,
                    message="No active matchup found for the current date",
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
                    return StreamerResp(
                        status=ApiStatus.ERROR,
                        message=f"Day {target_day} is out of bounds. Matchup has {game_span} days (0-{game_span - 1}).",
                        data=None
                    )
                effective_date = start_date + timedelta(days=target_day)
            else:
                effective_date = date.today()
                # In daily mode with no target_day, default to current day
                if mode == StreamerMode.DAILY:
                    target_day = current_day_index

            # Select scoring weights based on mode
            weights = (
                StreamerService.DAILY_WEIGHTS
                if mode == StreamerMode.DAILY
                else StreamerService.WEEK_WEIGHTS
            )

            # Get teams with B2B games
            teams_with_b2b = get_teams_with_b2b(effective_date)

            # Fetch free agents - route by provider
            # Pass team_id for Yahoo so tokens can be refreshed and persisted
            is_yahoo = league_info.provider == FantasyProvider.YAHOO
            if is_yahoo:
                fa_response = await YahooService.get_free_agents(league_info, fa_count, team_id)
            else:
                fa_response = await EspnService.get_free_agents(league_info, fa_count)

            if fa_response.status != ApiStatus.SUCCESS or not fa_response.data:
                return StreamerResp(
                    status=ApiStatus.ERROR,
                    message=f"Failed to fetch free agents: {fa_response.message}",
                    data=None
                )

            free_agents = fa_response.data

            # Fetch last n-day averages from our database
            # Yahoo uses name-based lookup, ESPN uses player ID
            if is_yahoo:
                player_lookups = [(fa.name, fa.team) for fa in free_agents]
                last_n_avgs_by_name = PlayerService.get_last_n_day_avg_batch_by_name(
                    player_lookups, days=avg_days
                )
            else:
                player_ids = [fa.player_id for fa in free_agents]
                last_n_avgs = PlayerService.get_last_n_day_avg_batch(player_ids, days=avg_days)

            # Build streamer list
            streamers: list[StreamerPlayerResp] = []

            for fa in free_agents:
                # Skip injured players if requested
                if exclude_injured and fa.injured:
                    continue

                # Get team schedule info
                team = fa.team
                team_has_b2b = has_remaining_b2b(team, effective_date)

                # Skip non-B2B teams if b2b_only is set
                if b2b_only and not team_has_b2b:
                    continue

                games_remaining = get_remaining_games(team, effective_date)
                game_days = get_remaining_game_days(team, effective_date)
                b2b_game_count = get_b2b_game_count(team, effective_date)

                # Skip players with no remaining games
                if games_remaining == 0:
                    continue

                # In daily mode, only include players with a game on the target day
                if mode == StreamerMode.DAILY and target_day not in game_days:
                    continue

                # Get last n-day average from our database
                # Yahoo uses name-based lookup, ESPN uses player ID
                if is_yahoo:
                    normalized_name = fa.name.lower().strip()
                    avg_points_last_n = last_n_avgs_by_name.get(normalized_name)
                else:
                    avg_points_last_n = last_n_avgs.get(fa.player_id)

                # Calculate streamer score
                streamer_score = StreamerService._calculate_streamer_score(
                    has_b2b=team_has_b2b,
                    games_remaining=games_remaining,
                    avg_points_last_n=avg_points_last_n,
                    b2b_game_count=b2b_game_count,
                    weights=weights
                )

                streamers.append(StreamerPlayerResp(
                    player_id=fa.player_id,
                    name=fa.name,
                    team=team,
                    valid_positions=fa.valid_positions,
                    avg_points_last_n=avg_points_last_n,
                    avg_points_season=fa.avg_points,
                    games_remaining=games_remaining,
                    has_b2b=team_has_b2b,
                    b2b_game_count=b2b_game_count,
                    game_days=game_days,
                    streamer_score=streamer_score,
                    injured=fa.injured,
                    injury_status=None  # Could be enhanced later
                ))

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
                    streamers=streamers
                )
            )

        except Exception as e:
            print(f"Error in find_streamers: {e}")
            import traceback
            traceback.print_exc()
            return StreamerResp(
                status=ApiStatus.ERROR,
                message="Internal server error while finding streamers",
                data=None
            )

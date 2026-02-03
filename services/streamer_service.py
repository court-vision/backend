from typing import Optional
from datetime import date, timedelta

from schemas.streamer import (
    StreamerResp,
    StreamerData,
    StreamerPlayerResp,
    StreamerReq
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

    # Ranking weights
    WEIGHT_B2B = 50.0           # Bonus for having B2B remaining
    WEIGHT_GAMES_REMAINING = 10.0  # Per game remaining
    WEIGHT_AVG_POINTS = 1.0     # Per point of last_7 average
    WEIGHT_B2B_GAMES = 5.0      # Per game that's part of a B2B

    @staticmethod
    def _calculate_streamer_score(
        has_b2b: bool,
        games_remaining: int,
        avg_points_last_7: Optional[float],
        b2b_game_count: int
    ) -> float:
        """
        Calculate the streaming score for a player.

        Score Components:
        1. B2B Bonus: +50 if team has remaining B2B games
        2. Games Remaining: +10 per remaining game
        3. Performance: +1 per point of last_7 average
        4. B2B Game Density: +5 per game that's part of a B2B

        Returns:
            The calculated streamer score.
        """
        score = 0.0

        if has_b2b:
            score += StreamerService.WEIGHT_B2B

        score += games_remaining * StreamerService.WEIGHT_GAMES_REMAINING

        if avg_points_last_7 is not None:
            score += avg_points_last_7 * StreamerService.WEIGHT_AVG_POINTS

        score += b2b_game_count * StreamerService.WEIGHT_B2B_GAMES

        return round(score, 1)

    @staticmethod
    async def find_streamers(
        league_info: LeagueInfo,
        fa_count: int = 50,
        exclude_injured: bool = True,
        b2b_only: bool = False,
        day: Optional[int] = None,
        avg_days: int = 7,
        team_id: Optional[int] = None
    ) -> StreamerResp:
        """
        Find and rank the best streaming candidates from free agents.

        Args:
            league_info: ESPN league credentials and team info.
            fa_count: Number of free agents to fetch (default 50).
            exclude_injured: Whether to exclude injured players (default True).
            b2b_only: Only show players on teams with remaining B2Bs (default False).
            day: Day index within the matchup (0-indexed). If None, uses current day.
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

            # If day is provided, use it; otherwise use the current day index
            if day is not None:
                # Validate day is within matchup bounds
                if day >= game_span:
                    return StreamerResp(
                        status=ApiStatus.ERROR,
                        message=f"Day {day} is out of bounds. Matchup has {game_span} days (0-{game_span - 1}).",
                        data=None
                    )
                current_day_index = day
                # Calculate effective date for schedule lookups
                effective_date = start_date + timedelta(days=day)
            else:
                current_day_index = matchup["current_day_index"]
                effective_date = date.today()

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
                    avg_points_last_7=avg_points_last_n,
                    b2b_game_count=b2b_game_count
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

            # Sort by has_b2b (True first), then by streamer_score (descending)
            streamers.sort(key=lambda x: (-x.has_b2b, -x.streamer_score))

            return StreamerResp(
                status=ApiStatus.SUCCESS,
                message=f"Found {len(streamers)} streaming candidates",
                data=StreamerData(
                    matchup_number=matchup_number,
                    current_day_index=current_day_index,
                    avg_days=avg_days,
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

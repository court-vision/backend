"""Yahoo matchup retrieval and projection assembly."""

from core.errors import BadRequestError
from db.base import run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.matchup import (
    CategoryTeamScore, MatchupData, MatchupPlayerResp, MatchupResp, MatchupTeamResp,
)
from services.player_service import _normalize_name
from services.player_value_service import PlayerValueService
from services.providers.http import provider_get
from services.schedule_service import get_current_matchup, get_nba_today, get_remaining_games, season_day
from services.scoring.providers.yahoo_settings import parse_yahoo_matchup_categories
from services.scoring.resolver import ResolvedScoring, resolve_scoring
from services.yahoo.rosters import YahooRosterService
from utils.yahoo_helpers import (
    YAHOO_POSITION_MAP, normalize_team_abbr, parse_yahoo_player_positions, parse_yahoo_team_key,
)

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooMatchupService(YahooRosterService):
    @classmethod
    async def get_matchup_data(cls, league_info: LeagueInfo, avg_window: str = "season", team_id: int | None = None, scoring: ResolvedScoring | None = None) -> MatchupResp:
        """
        Get current matchup data from Yahoo API.

        Args:
            league_info: League info with Yahoo credentials
            avg_window: Averaging window for projections
            team_id: Optional team ID to persist refreshed tokens

        Returns:
            MatchupResp with current matchup data
        """
        from services.schedule_service import get_matchup_dates

        scoring = scoring or resolve_scoring(None)
        access_token = await cls._ensure_valid_token(league_info, team_id)
        team_key = league_info.yahoo_team_key

        if not team_key:
            raise BadRequestError("LEAGUE_VALIDATION_FAILED", "No Yahoo team key provided")

        parsed_key = parse_yahoo_team_key(team_key)

        # Fetch current matchup
        endpoint = f"{YAHOO_API_BASE}/team/{team_key}/matchups?format=json"
        headers = cls._get_headers(access_token)

        data = await provider_get("yahoo", endpoint, headers=headers, expect_key="fantasy_content")

        # Parse Yahoo matchup response to find current matchup and opponent
        fantasy_content = data.get("fantasy_content", {})
        team_data = fantasy_content.get("team", [])

        current_matchup = None
        matchup_week = 1
        our_score = 0.0
        opponent_score = 0.0
        opponent_team_key = None
        opponent_name = "Opponent"

        for item in team_data:
            if isinstance(item, dict) and "matchups" in item:
                matchups = item["matchups"]
                # Handle both dict and list responses from Yahoo API
                if isinstance(matchups, dict):
                    matchups_iter = matchups.items()
                elif isinstance(matchups, list):
                    matchups_iter = enumerate(matchups)
                else:
                    continue

                # Find the current in-progress matchup by status ("midevent"),
                # rather than taking the first matchup which would be week 1.
                matchups_list = list(matchups_iter)

                target_matchup_info = None
                for matchup_key, matchup_data in matchups_list:
                    if matchup_key == "count":
                        continue
                    if isinstance(matchup_data, dict) and "matchup" in matchup_data:
                        mi = matchup_data["matchup"]
                        if mi.get("status") == "midevent":
                            target_matchup_info = mi
                            break

                # Fallback: take the last matchup (most recent) if none is midevent
                if not target_matchup_info:
                    for matchup_key, matchup_data in reversed(matchups_list):
                        if matchup_key == "count":
                            continue
                        if isinstance(matchup_data, dict) and "matchup" in matchup_data:
                            target_matchup_info = matchup_data["matchup"]
                            break

                if target_matchup_info:
                    matchup_week = int(target_matchup_info.get("week", 1))

                    # Parse teams in matchup
                    teams_in_matchup = target_matchup_info.get("0", {}).get("teams", {})
                    if isinstance(teams_in_matchup, dict):
                        teams_iter = teams_in_matchup.items()
                    elif isinstance(teams_in_matchup, list):
                        teams_iter = enumerate(teams_in_matchup)
                    else:
                        teams_iter = []

                    for t_key, t_data in teams_iter:
                        if t_key == "count":
                            continue
                        if isinstance(t_data, dict) and "team" in t_data:
                            team_info = t_data["team"]
                            team_details = {}
                            team_points = 0.0

                            # Parse team details from nested structure
                            for t_item in team_info:
                                if isinstance(t_item, list):
                                    for sub in t_item:
                                        if isinstance(sub, dict):
                                            team_details.update(sub)
                                elif isinstance(t_item, dict):
                                    if "team_points" in t_item:
                                        tp = t_item["team_points"]
                                        team_points = float(tp.get("total", 0))
                                    else:
                                        team_details.update(t_item)

                            t_team_key = team_details.get("team_key", "")
                            t_name = team_details.get("name", "Unknown")

                            if t_team_key == team_key:
                                our_score = team_points
                            else:
                                opponent_team_key = t_team_key
                                opponent_name = t_name
                                opponent_score = team_points

                    current_matchup = target_matchup_info

        if not current_matchup:
            # An empty state, not a failure (bye week / offseason): 200 with data: null
            return MatchupResp(
                status=ApiStatus.SUCCESS,
                message="No current matchup found",
                data=None
            )

        # Yahoo sends the matchup's own dates; fall back to our calendar by week number
        # (Yahoo's week numbering need not match ours, e.g. around the All-Star break).
        matchup_dates = cls._matchup_dates_from_payload(current_matchup) or get_matchup_dates(matchup_week)
        matchup_start = matchup_dates[0].isoformat() if matchup_dates else ""
        matchup_end = matchup_dates[1].isoformat() if matchup_dates else ""
        _week = get_current_matchup(matchup_dates[0]) if matchup_dates else None
        schedule_week = _week["matchup_number"] if _week else None

        # Fetch our team roster
        our_roster = await cls._fetch_roster_for_matchup(
            team_key, access_token, avg_window, scoring
        )

        # Fetch opponent roster if we have their team key
        opponent_roster = []
        if opponent_team_key:
            opponent_roster = await cls._fetch_roster_for_matchup(
                opponent_team_key, access_token, avg_window, scoring
            )

        # Calculate projected scores
        def calc_projected(roster: list[MatchupPlayerResp], current: float) -> float:
            future_pts = 0.0
            for p in roster:
                if p.lineup_slot not in ("IR", "IL", "IL+", "BE") and not p.injured:
                    future_pts += p.avg_points * p.games_remaining
            return current + future_pts

        category_comparison = projected_category_comparison = None
        your_cat_schema = opp_cat_schema = None
        if scoring.is_categories and scoring.categories is not None:
            from services.espn_service import EspnService
            cats = scoring.categories
            parsed = parse_yahoo_matchup_categories(current_matchup, team_key, cats.categories)
            if parsed is None:
                from services.scoring.models import CategoryTeamScoreData
                your_cat, opp_cat = CategoryTeamScoreData(totals={}), CategoryTeamScoreData(totals={})
            else:
                your_cat, opp_cat = parsed
            current_cmp = cats.compare(your_cat.totals, opp_cat.totals)
            if (your_cat.wins + your_cat.losses + your_cat.ties) == 0:
                your_cat.wins, your_cat.losses, your_cat.ties = current_cmp.wins, current_cmp.losses, current_cmp.ties
                opp_cat.wins, opp_cat.losses, opp_cat.ties = current_cmp.losses, current_cmp.wins, current_cmp.ties

            def proj_inputs(roster):
                lines = projected_lines.get(id(roster), {})
                out = []
                for p in roster:
                    line = lines.get(_normalize_name(p.name))
                    counts = p.lineup_slot not in ("IR", "IL", "IL+", "BE") and not p.injured
                    if line is not None:
                        out.append((line, p.games_remaining, counts))
                return out

            projected_lines = {
                id(our_roster): await run_db(
                    "yahoo.matchup_our_lines", PlayerValueService.rolling_lines_by_name,
                    [(p.name, p.team) for p in our_roster], days=7,
                ),
                id(opponent_roster): await run_db(
                    "yahoo.matchup_opponent_lines", PlayerValueService.rolling_lines_by_name,
                    [(p.name, p.team) for p in opponent_roster], days=7,
                ),
            }
            proj_cmp = cats.compare(cats.project(your_cat, proj_inputs(our_roster)),
                                    cats.project(opp_cat, proj_inputs(opponent_roster)))
            category_comparison = EspnService._comparison_schema(current_cmp)
            projected_category_comparison = EspnService._comparison_schema(proj_cmp)
            your_cat_schema = CategoryTeamScore(**your_cat.__dict__)
            opp_cat_schema = CategoryTeamScore(**opp_cat.__dict__)
            our_score, opponent_score = float(your_cat.wins), float(your_cat.losses)
            our_projected, opponent_projected = float(proj_cmp.wins), float(proj_cmp.losses)
            projected_margin = float(proj_cmp.wins - proj_cmp.losses)
        else:
            our_projected = calc_projected(our_roster, our_score)
            opponent_projected = calc_projected(opponent_roster, opponent_score)
            projected_margin = round(abs(our_projected - opponent_projected), 2)

        # Determine winner
        if our_projected > opponent_projected:
            projected_winner = league_info.team_name
        elif opponent_projected > our_projected:
            projected_winner = opponent_name
        else:
            projected_winner = "Tie"

        matchup_data = MatchupData(
            matchup_period=matchup_week,
            schedule_week=schedule_week,
            matchup_period_start=matchup_start,
            matchup_period_end=matchup_end,
            your_team=MatchupTeamResp(
                team_name=league_info.team_name,
                team_id=int(parsed_key.get("team_id", 0)),
                current_score=our_score,
                projected_score=round(our_projected, 2),
                roster=our_roster,
                categories=your_cat_schema,
            ),
            opponent_team=MatchupTeamResp(
                team_name=opponent_name,
                team_id=0,
                current_score=opponent_score,
                projected_score=round(opponent_projected, 2),
                roster=opponent_roster,
                categories=opp_cat_schema,
            ),
            projected_winner=projected_winner,
            projected_margin=projected_margin,
            # Yahoo exposes only week_start/week_end -- there is no day-granular
            # watermark to read. Derive it from our own calendar on the fantasy
            # day (2 AM ET), which is when a provider's day flips; using the
            # 6 AM ET game-date rule here would report the previous day all
            # morning and make every Yahoo baseline look permanently stale.
            scoring_period_id=season_day(get_nba_today()),
            scoring_period_source="calendar",
            scoring_format=scoring.format,
            settings_synced=scoring.settings_synced,
            category_comparison=category_comparison,
            projected_category_comparison=projected_category_comparison,
        )

        return MatchupResp(
            status=ApiStatus.SUCCESS,
            message="Yahoo matchup data fetched successfully",
            data=matchup_data
        )


    @classmethod
    async def _fetch_roster_for_matchup(cls, 
        team_key: str,
        access_token: str,
        avg_window: str,
        scoring: ResolvedScoring | None = None,
    ) -> list[MatchupPlayerResp]:
        """
        Fetch a team's roster with stats for matchup display.

        Args:
            team_key: Yahoo team key
            access_token: Valid Yahoo access token
            avg_window: Averaging window for stats
            scoring: The league's resolved scoring (default points scoring when omitted)

        Returns:
            List of MatchupPlayerResp for the team roster
        """
        endpoint = f"{YAHOO_API_BASE}/team/{team_key}/roster/players?format=json"
        headers = cls._get_headers(access_token)

        data = await provider_get("yahoo", endpoint, headers=headers, expect_key="fantasy_content")

        # Parse roster
        parsed_players = []
        fantasy_content = data.get("fantasy_content", {})
        team = fantasy_content.get("team", [])

        for item in team:
            if isinstance(item, dict) and "roster" in item:
                roster = item["roster"]
                players_data = roster.get("0", {}).get("players", {})

                if isinstance(players_data, dict):
                    players_iter = players_data.items()
                elif isinstance(players_data, list):
                    players_iter = enumerate(players_data)
                else:
                    continue

                for player_key, player_data in players_iter:
                    if player_key == "count":
                        continue
                    if isinstance(player_data, dict) and "player" in player_data:
                        player_info = player_data["player"]
                        player_details = {}
                        eligible_positions = []
                        selected_position = "UT"

                        for player_item in player_info:
                            if isinstance(player_item, list):
                                for sub_item in player_item:
                                    if isinstance(sub_item, dict):
                                        if "eligible_positions" in sub_item:
                                            eligible_positions = sub_item["eligible_positions"]
                                        elif "selected_position" in sub_item:
                                            sp = sub_item["selected_position"]
                                            if isinstance(sp, list) and len(sp) > 0:
                                                selected_position = sp[0].get("position", "UT")
                                            elif isinstance(sp, dict):
                                                selected_position = sp.get("position", "UT")
                                        else:
                                            player_details.update(sub_item)
                            elif isinstance(player_item, dict):
                                if "selected_position" in player_item:
                                    sp = player_item["selected_position"]
                                    if isinstance(sp, list) and len(sp) > 0:
                                        selected_position = sp[0].get("position", "UT")
                                    elif isinstance(sp, dict):
                                        selected_position = sp.get("position", "UT")
                                else:
                                    player_details.update(player_item)

                        player_id = int(player_details.get("player_id", 0))
                        name = player_details.get("name", {})
                        if isinstance(name, dict):
                            full_name = name.get("full", "Unknown")
                        else:
                            full_name = str(name)

                        team_abbrev = normalize_team_abbr(
                            player_details.get("editorial_team_abbr", "FA").upper()
                        )

                        # Get primary position
                        positions = parse_yahoo_player_positions(eligible_positions)
                        primary_pos = positions[0] if positions else "UT"

                        # Normalize lineup slot
                        lineup_slot = YAHOO_POSITION_MAP.get(selected_position, selected_position)

                        status = player_details.get("status", "")
                        injured = status in ("IL", "IL+", "O", "GTD", "DTD")
                        injury_status = status if status else None

                        parsed_players.append({
                            "player_id": player_id,
                            "name": full_name,
                            "team": team_abbrev,
                            "position": primary_pos,
                            "lineup_slot": lineup_slot,
                            "injured": injured,
                            "injury_status": injury_status,
                        })

        # Value players from our stored stats under the league's scoring (see get_team_data)
        player_lookups = [(p["name"], p["team"]) for p in parsed_players]
        scoring = scoring or resolve_scoring(None)
        name_to_value = await run_db(
            "yahoo.value_matchup_roster", PlayerValueService.avg_points_for,
            scoring, names=player_lookups,
        )

        # Build MatchupPlayerResp list
        roster = []
        for p in parsed_players:
            normalized_name = _normalize_name(p["name"])
            valued = name_to_value.get(normalized_name)
            avg_points = valued.value if valued is not None and valued.value is not None else 0.0
            games_remaining = get_remaining_games(p["team"])

            roster.append(MatchupPlayerResp(
                player_id=p["player_id"],
                name=p["name"],
                team=p["team"],
                position=p["position"],
                lineup_slot=p["lineup_slot"],
                avg_points=avg_points,
                projected_points=round(avg_points * games_remaining, 2),
                games_remaining=games_remaining,
                injured=p["injured"],
                injury_status=p["injury_status"],
            ))

        return roster

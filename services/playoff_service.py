from db.models.nba.playoff_series import PlayoffSeries
from schemas.common import ApiStatus
from schemas.playoff import PlayoffBracketResp, PlayoffBracketData, PlayoffRound, PlayoffSeriesResp


class PlayoffService:

    @staticmethod
    async def get_bracket(season: str | None = None) -> PlayoffBracketResp:
        """
        Return the current playoff bracket from nba.playoff_series.

        If season is None, uses the most recent season that has data.
        """
        query = PlayoffSeries.select()
        if season:
            query = query.where(PlayoffSeries.season == season)
        else:
            # Pick most recent season with data
            latest = (
                PlayoffSeries.select(PlayoffSeries.season)
                .order_by(PlayoffSeries.season.desc())
                .first()
            )
            if not latest:
                return PlayoffBracketResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No playoff bracket data found",
                    data=None,
                )
            query = query.where(PlayoffSeries.season == latest.season)
            season = latest.season

        records = list(query.order_by(PlayoffSeries.round_num, PlayoffSeries.series_id))

        if not records:
            return PlayoffBracketResp(
                status=ApiStatus.NOT_FOUND,
                message=f"No bracket data for season {season}",
                data=None,
            )

        # Group by round
        rounds_map: dict[int, list[PlayoffSeries]] = {}
        for r in records:
            rounds_map.setdefault(r.round_num, []).append(r)

        rounds = []
        for round_num in sorted(rounds_map):
            round_name = {1: "First Round", 2: "Conference Semifinals", 3: "Conference Finals", 4: "NBA Finals"}.get(round_num, f"Round {round_num}")
            series_list = [
                PlayoffSeriesResp(
                    series_id=s.series_id,
                    conference=s.conference,
                    round_num=s.round_num,
                    top_seed_team_id=s.top_seed_team_id,
                    top_seed_name=s.top_seed_name,
                    top_seed_abbr=s.top_seed_abbr,
                    top_seed_wins=s.top_seed_wins,
                    bottom_seed_team_id=s.bottom_seed_team_id,
                    bottom_seed_name=s.bottom_seed_name,
                    bottom_seed_abbr=s.bottom_seed_abbr,
                    bottom_seed_wins=s.bottom_seed_wins,
                    series_complete=s.series_complete,
                    series_leader_abbr=s.series_leader_abbr,
                    updated_at=s.updated_at.isoformat() if s.updated_at else None,
                )
                for s in rounds_map[round_num]
            ]
            rounds.append(PlayoffRound(round_num=round_num, round_name=round_name, series=series_list))

        return PlayoffBracketResp(
            status=ApiStatus.SUCCESS,
            message=f"Playoff bracket for {season}",
            data=PlayoffBracketData(season=season, rounds=rounds),
        )

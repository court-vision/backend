"""Persisted matchup history, daily drill-down, and season summaries."""

from datetime import date as date_type, timedelta

from core.errors import NotFoundError
from db.base import db_operation, run_db
from db.models.stats.daily_matchup_score import DailyMatchupScore
from schemas.common import ApiStatus
from schemas.matchup import (
    DailyMatchupResp,
    DailyScorePoint,
    MatchupScoreHistory,
    MatchupScoreHistoryResp,
    SeasonSummaryData,
    SeasonSummaryResp,
    WeekResult,
    WeeklyMatchupData,
    WeeklyMatchupResp,
)
from services.matchup_days import build_day, make_nba_id_resolver, nba_today as _nba_today
from services.scoring.resolver import resolve_scoring_for_team


class MatchupHistoryService:
    """Mixin whose caller supplies ``get_matchup_by_team_id`` orchestration."""

    @staticmethod
    @db_operation("matchups.score_history")
    def get_score_history(team_id: int, matchup_period: int | None = None) -> MatchupScoreHistoryResp:
        query = DailyMatchupScore.select().where(DailyMatchupScore.team_id == team_id)
        if matchup_period is not None:
            query = query.where(DailyMatchupScore.matchup_period == matchup_period)
        else:
            latest = (
                DailyMatchupScore.select(DailyMatchupScore.matchup_period)
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
        return MatchupScoreHistoryResp(
            status=ApiStatus.SUCCESS,
            message="Score history retrieved successfully",
            data=MatchupScoreHistory(
                team_id=team_id,
                team_name=first_record.team_name,
                opponent_team_name=first_record.opponent_team_name,
                matchup_period=first_record.matchup_period,
                history=history,
                scoring_format="categories" if any(r.category_scores for r in records) else "points",
            ),
        )

    @staticmethod
    def _build_daily_from_db(md, team_id: int, target_date: date_type, period_start: date_type) -> DailyMatchupResp:
        from db.models.nba.games import Game
        from db.models.nba.live_player_stats import LivePlayerStats
        from db.models.nba.player_game_stats import PlayerGameStats

        today = _nba_today()
        scoring = resolve_scoring_for_team(team_id)
        all_roster = md.your_team.roster + md.opponent_team.roster
        resolve = make_nba_id_resolver(all_roster)
        games_on_date = list(Game.get_games_on_date(target_date))
        nba_id_to_stats: dict[int, object] = {}
        if target_date <= today:
            nba_ids = [nid for nid in (resolve(p) for p in all_roster) if nid is not None]
            if nba_ids:
                for stat in PlayerGameStats.select().where(
                    (PlayerGameStats.player_id.in_(nba_ids)) & (PlayerGameStats.game_date == target_date)
                ):
                    nba_id_to_stats[stat.player_id] = stat
                if target_date == today:
                    for stat in LivePlayerStats.select().where(
                        (LivePlayerStats.player_id.in_(nba_ids)) & (LivePlayerStats.game_date == target_date)
                    ):
                        nba_id_to_stats.setdefault(stat.player_id, stat)
        day = build_day(md, target_date, today, period_start, nba_id_to_stats, games_on_date, resolve, scoring)
        return DailyMatchupResp(status=ApiStatus.SUCCESS, message="Daily matchup data fetched successfully", data=day)

    @staticmethod
    def _build_weekly_from_db(md, team_id: int, dates: list[date_type], period_start: date_type) -> WeeklyMatchupResp:
        from db.models.nba.games import Game
        from db.models.nba.live_player_stats import LivePlayerStats
        from db.models.nba.player_game_stats import PlayerGameStats

        today = _nba_today()
        scoring = resolve_scoring_for_team(team_id)
        all_roster = md.your_team.roster + md.opponent_team.roster
        resolve = make_nba_id_resolver(all_roster)
        all_nba_ids = list({nid for nid in (resolve(p) for p in all_roster) if nid is not None})
        past_and_today = [day for day in dates if day <= today]
        stats_by_player_date: dict[tuple[int, date_type], object] = {}
        if all_nba_ids and past_and_today:
            for stat in PlayerGameStats.select().where(
                (PlayerGameStats.player_id.in_(all_nba_ids)) & (PlayerGameStats.game_date.in_(past_and_today))
            ):
                stats_by_player_date[(stat.player_id, stat.game_date)] = stat
        live_by_player: dict[int, object] = {}
        if all_nba_ids and today in dates:
            for stat in LivePlayerStats.select().where(
                (LivePlayerStats.player_id.in_(all_nba_ids)) & (LivePlayerStats.game_date == today)
            ):
                live_by_player[stat.player_id] = stat
        games_by_date: dict[date_type, list] = {}
        for game in Game.select().where(Game.game_date.in_(dates)):
            games_by_date.setdefault(game.game_date, []).append(game)

        def stats_for_day(day: date_type) -> dict[int, object]:
            result = {}
            for nba_id in all_nba_ids:
                stat = stats_by_player_date.get((nba_id, day))
                if stat is None and day == today:
                    stat = live_by_player.get(nba_id)
                if stat is not None:
                    result[nba_id] = stat
            return result

        days = [
            build_day(md, day, today, period_start, stats_for_day(day), games_by_date.get(day, []), resolve, scoring)
            for day in dates
        ]
        return WeeklyMatchupResp(
            status=ApiStatus.SUCCESS,
            message="Weekly matchup data fetched successfully",
            data=WeeklyMatchupData(matchup_period=md.matchup_period, days=days),
        )

    @classmethod
    async def get_daily_matchup(cls, user_id: int, team_id: int, target_date: date_type) -> DailyMatchupResp:
        matchup = await cls.get_matchup_by_team_id(user_id, team_id, avg_window="season")
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
        if target_date < period_start or target_date > period_end:
            return DailyMatchupResp(
                status=ApiStatus.BAD_REQUEST,
                message=f"Date {target_date} is outside matchup period {period_start} to {period_end}",
                data=None,
            )
        return await run_db("matchups.daily", cls._build_daily_from_db, md, team_id, target_date, period_start)

    @classmethod
    async def get_weekly_matchup(cls, user_id: int, team_id: int) -> WeeklyMatchupResp:
        matchup = await cls.get_matchup_by_team_id(user_id, team_id, avg_window="season")
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
        dates = [period_start + timedelta(days=i) for i in range((period_end - period_start).days + 1)]
        return await run_db("matchups.weekly", cls._build_weekly_from_db, md, team_id, dates, period_start)

    @staticmethod
    @db_operation("matchups.season_summary")
    def get_season_summary(team_id: int) -> SeasonSummaryResp:
        records = list(
            DailyMatchupScore.select()
            .where(DailyMatchupScore.team_id == team_id)
            .order_by(DailyMatchupScore.matchup_period, DailyMatchupScore.date)
        )
        if not records:
            return SeasonSummaryResp(
                status=ApiStatus.NOT_FOUND,
                message=f"No season data found for team {team_id}",
                data=None,
            )

        periods: dict[int, list] = {}
        for record in records:
            periods.setdefault(record.matchup_period, []).append(record)

        wins = losses = 0
        total_pf = total_pa = 0.0
        best_week: WeekResult | None = None
        worst_week: WeekResult | None = None
        weeks: list[WeekResult] = []
        any_categories = False
        for period in sorted(periods):
            last = periods[period][-1]
            pf, pa = float(last.current_score), float(last.opponent_current_score)
            won = pf > pa
            cs = last.category_scores if isinstance(last.category_scores, dict) else None
            any_categories = any_categories or bool(cs)
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

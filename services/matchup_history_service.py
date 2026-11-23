"""Persisted matchup history, daily drill-down, and season summaries."""

from datetime import date as date_type, timedelta

from core.errors import NotFoundError
from core.logging import get_logger
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
from schemas.common import FantasyProvider
from services.matchup_days import (
    DayRosters, RosterSide, build_day, fantasy_today as _fantasy_today, make_nba_id_resolver, matchup_today,
)
from services.scoring.resolver import resolve_scoring_for_team
from services.team_service import TeamService


log = get_logger("matchup_history")


def _matchup_today(md) -> date_type:
    """ESPN's own day for this matchup read, else the 3 AM ET fantasy day."""
    return matchup_today(md, _fantasy_today)


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
    def _stat_rows(nba_ids: list[int], days: list[date_type]) -> dict[tuple[int, date_type], object]:
        """Box scores by (nba id, game date) for days already played (or on).

        The post-game run's row first. A night it has not written yet (from
        ESPN's 3 AM ET rollover until that run, the day is past but its rows may
        not be there) keeps the live pipeline's rows, which stay until the next
        night's games start writing; today's games are only ever live rows.
        """
        from db.models.nba.live_player_stats import LivePlayerStats
        from db.models.nba.player_game_stats import PlayerGameStats

        rows: dict[tuple[int, date_type], object] = {}
        if not nba_ids or not days:
            return rows
        for stat in PlayerGameStats.select().where(
            (PlayerGameStats.player_id.in_(nba_ids)) & (PlayerGameStats.game_date.in_(days))
        ):
            rows[(stat.player_id, stat.game_date)] = stat
        for stat in LivePlayerStats.select().where(
            (LivePlayerStats.player_id.in_(nba_ids)) & (LivePlayerStats.game_date.in_(days))
        ):
            rows.setdefault((stat.player_id, stat.game_date), stat)
        return rows

    @staticmethod
    def _build_daily_from_db(md, team_id: int, target_date: date_type, period_start: date_type,
                             today: date_type, rosters: DayRosters | None = None) -> DailyMatchupResp:
        from db.models.nba.games import Game

        scoring = resolve_scoring_for_team(team_id)
        sides = rosters or DayRosters()
        your_players = sides.your.players if sides.your.players is not None else md.your_team.roster
        opp_players = sides.opp.players if sides.opp.players is not None else md.opponent_team.roster
        all_roster = list(your_players) + list(opp_players)
        resolve = make_nba_id_resolver(all_roster)
        games_on_date = list(Game.get_games_on_date(target_date))
        nba_id_to_stats: dict[int, object] = {}
        if target_date <= today:
            nba_ids = [nid for nid in (resolve(p) for p in all_roster) if nid is not None]
            rows = MatchupHistoryService._stat_rows(nba_ids, [target_date])
            nba_id_to_stats = {nid: stat for (nid, _), stat in rows.items()}
        day = build_day(md, target_date, today, period_start, nba_id_to_stats, games_on_date, resolve, scoring,
                        rosters=sides)
        return DailyMatchupResp(status=ApiStatus.SUCCESS, message="Daily matchup data fetched successfully", data=day)

    @staticmethod
    def _build_weekly_from_db(md, team_id: int, dates: list[date_type], period_start: date_type,
                              today: date_type, rosters_by_day: dict[date_type, DayRosters] | None = None
                              ) -> WeeklyMatchupResp:
        from db.models.nba.games import Game

        scoring = resolve_scoring_for_team(team_id)
        by_day = rosters_by_day or {}
        all_roster = list(md.your_team.roster) + list(md.opponent_team.roster)
        for sides in by_day.values():
            for side in (sides.your, sides.opp):
                if side.players is not None:
                    all_roster.extend(side.players)
        resolve = make_nba_id_resolver(all_roster)
        all_nba_ids = list({nid for nid in (resolve(p) for p in all_roster) if nid is not None})
        played = [day for day in dates if day <= today]
        stat_rows = MatchupHistoryService._stat_rows(all_nba_ids, played)
        games_by_date: dict[date_type, list] = {}
        for game in Game.select().where(Game.game_date.in_(dates)):
            games_by_date.setdefault(game.game_date, []).append(game)

        def stats_for_day(day: date_type) -> dict[int, object]:
            return {nid: stat for (nid, d), stat in stat_rows.items() if d == day}

        days = [
            build_day(md, day, today, period_start, stats_for_day(day), games_by_date.get(day, []), resolve, scoring,
                      rosters=by_day.get(day))
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
        # Which day is today, and so whose roster a day shows, is ESPN's
        # question: once ESPN has rolled past a day (3 AM ET) the current
        # roster is already the next day's, so the day's own lineup is used and
        # the day is past. Its box scores are last night's live rows until the
        # post-game run writes them (`_stat_rows`).
        today = _matchup_today(md)
        rosters = None
        if target_date < today:
            rosters = (await cls._past_rosters(team_id, md, [target_date])).get(target_date)
        return await run_db("matchups.daily", cls._build_daily_from_db, md, team_id, target_date, period_start,
                            today, rosters)

    @staticmethod
    async def _past_rosters(team_id: int, md, days: list[date_type]) -> dict[date_type, DayRosters]:
        """Both teams' rosters as they stood on finished days: the nightly
        lineup snapshots first, ESPN's own per-day history for the latest day
        when its row is not there yet, and today's roster (labelled `current`)
        as the last resort. Any failure here falls back rather than failing
        the days."""
        from services.lineup_snapshot_service import LineupSnapshotService

        if not days:
            return {}
        try:
            league_info = await TeamService.credentials_for(team_id)
            if league_info.provider != FantasyProvider.ESPN:
                return {}
            sides = {
                md.your_team.team_id: md.your_team.team_name,
                md.opponent_team.team_id: md.opponent_team.team_name,
            }
            found = await LineupSnapshotService.rosters_for_matchup_days(league_info, sides, days)
        except Exception as exc:  # the days still render on today's roster
            log.warning("matchup_past_rosters_unavailable", team_id=team_id,
                        dates=[str(d) for d in days], error=str(exc))
            return {}

        def side(snapshot):
            if snapshot is None:
                return RosterSide()
            return RosterSide(players=list(snapshot.players), source=snapshot.source, captured_at=snapshot.captured_at)

        return {
            day: DayRosters(your=side(by_team.get(md.your_team.team_id)), opp=side(by_team.get(md.opponent_team.team_id)))
            for day, by_team in found.items()
        }

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
        # The same today as the team's ESPN board, and the past days on the
        # lineups that counted, as /matchups/daily shows them.
        today = _matchup_today(md)
        rosters = await cls._past_rosters(team_id, md, [d for d in dates if d < today])
        return await run_db("matchups.weekly", cls._build_weekly_from_db, md, team_id, dates, period_start,
                            today, rosters)

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

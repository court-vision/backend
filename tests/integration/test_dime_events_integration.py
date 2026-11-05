"""
Integration: Dime's event check-ins and the news reads against the real schema (migration
0045 over 0035).

An event is recorded once per verdict change and once per news item and player (the partial
unique indexes); its shape check; a check-in takes the team's pending events and gives them
back when it fails to start; what can no longer be answered is dropped; the facts read
behind data-platform's keys; event check-ins outside the once-a-day slot index; the news
reads (a player's, the league's by kind, team, roster and time; the newest item; the
verdict for today or the next game); and the rollback.
"""

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from core.settings import settings
from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.autopilot import AiCheckin, AutopilotEvent
from db.models.nba import NBATeam, NewsItem, NewsItemPlayer, NewsItemTeam, Player, PlayerAvailability
from db.models.nba.games import Game
from db.models.teams import Team
from db.models.users import User
from services import news_service
from services.ai import checkins, events

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 10)
GAME = "0022600150"
BRUNSON, HART = 1628973, 1628404
NOW = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)      # 5 PM ET; the Knicks tip at 7:30


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "dime_events_enabled", True)


@pytest.fixture
def team(integration_db):
    user = User.create(email="events@courtvision.dev", clerk_user_id="user_events", created_at=NOW)
    info = {"provider": "espn", "league_id": 1234, "team_name": "Mine", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="1234Mine", league_info=json.dumps(info))


@pytest.fixture
def night(integration_db):
    for tri in ("NYK", "BOS"):
        NBATeam.get_or_create(id=tri, defaults={"name": tri, "city": tri, "conference": "East", "division": "Atlantic"})
    Player.create(id=BRUNSON, espn_id=3934672, name="Jalen Brunson", name_normalized="jalen brunson",
                  created_at=NOW, updated_at=NOW)
    Player.create(id=HART, espn_id=3062679, name="Josh Hart", name_normalized="josh hart",
                  created_at=NOW, updated_at=NOW)
    Game.create(game_id=GAME, game_date=DAY, season="2026-27", home_team="NYK", away_team="BOS", status="scheduled",
                start_time_et=time(19, 30))


def _verdict(pid, verdict, previous, changed_at, game_id=GAME, day=DAY):
    return PlayerAvailability.create(player=pid, game_date=day, game_id=game_id, verdict=verdict,
                                     previous_verdict=previous, decided_by="espn_fantasy", reason="Left Knee",
                                     changed_at=changed_at, updated_at=changed_at)


def _item(source_item_id, kind="availability", published=NOW, players=(BRUNSON,), teams=("NYK",),
          headline="Brunson (knee) ruled out Tuesday", source="rotowire_rss"):
    item = NewsItem.create(source=source, source_item_id=source_item_id, published_at=published, fetched_at=published,
                           headline=headline, body="He will miss the game.", kind=kind)
    for pid in players:
        NewsItemPlayer.create(news_item=item, player=pid, linked_by="name")
    for tri in teams:
        NewsItemTeam.create(news_item=item, team=tri)
    return item


def _fact(pid=BRUNSON, changed=NOW, **kw):
    fields = dict(source=events.AVAILABILITY, player_id=pid, summary="Jalen Brunson ruled out", game_date=DAY,
                  game_id=GAME, tip_at=datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc), verdict="out",
                  previous_verdict="questionable", changed_at=changed)
    fields.update(kw)
    return events.Fact(**fields)


def _record(team, found, now=NOW):
    return run(events.record(team.team_id, team.user_id_id, DAY, found, now))


def _claim(team, kind=checkins.EVENT, state="running"):
    return run(checkins._claim(team.team_id, team.user_id_id, kind, DAY, NOW, manual=False, now=NOW, state=state))


class TestEvents:
    def test_each_verdict_change_and_news_item_is_recorded_once(self, team, night):
        assert _record(team, [_fact()]) == 1
        assert _record(team, [_fact()]) == 0                                       # the same change again
        assert _record(team, [_fact(changed=NOW + timedelta(minutes=8), verdict="available")]) == 1
        news = _item("rw-1")
        fact = events.Fact(source=events.NEWS, player_id=BRUNSON, summary="Brunson: ruled out (Rotowire)",
                           news_item_id=news.id, news_kind="availability")
        assert _record(team, [fact, fact]) == 1
        assert _record(team, [events.Fact(source=events.NEWS, player_id=HART, summary="Hart too",
                                          news_item_id=news.id, news_kind="availability")]) == 1
        assert AutopilotEvent.select().where(AutopilotEvent.state == "pending").count() == 4

    def test_an_event_has_the_shape_its_source_needs(self, team, night):
        with pytest.raises(IntegrityError), db.atomic():
            AutopilotEvent.create(team_id=team.team_id, user_id=team.user_id_id, source="availability",
                                  player_id=BRUNSON, nba_date=DAY, summary="no verdict", received_at=NOW)
        with pytest.raises(IntegrityError), db.atomic():
            AutopilotEvent.create(team_id=team.team_id, user_id=team.user_id_id, source="news", player_id=BRUNSON,
                                  nba_date=DAY, summary="no item", received_at=NOW)
        with pytest.raises(IntegrityError), db.atomic():
            AutopilotEvent.create(team_id=team.team_id, user_id=team.user_id_id, source="news", player_id=BRUNSON,
                                  nba_date=DAY, summary="", news_item_id=1, received_at=NOW)

    def test_a_check_in_takes_them_and_one_that_fails_to_start_gives_them_back(self, team, night, on):
        _record(team, [_fact(), _fact(pid=HART)])
        checkin_id = _claim(team)
        taken = run(events.take(team.team_id, checkin_id, NOW))
        assert [e.player_id for e in taken] == [BRUNSON, HART]
        assert all(e.state == "taken" and e.checkin_id_id == checkin_id for e in taken)
        assert run(events.pending(team.team_id)) == []
        assert run(events.take(team.team_id, checkin_id, NOW)) == []                 # nothing left to take
        assert run(events.release(checkin_id)) == 2
        assert [e.player_id for e in run(events.pending(team.team_id))] == [BRUNSON, HART]

    def test_a_check_in_s_events_show_with_it(self, team, night, on):
        _record(team, [_fact()])
        checkin_id = _claim(team)
        run(events.take(team.team_id, checkin_id, NOW))
        run(checkins._settle(checkin_id, "done", None, None, 0, NOW))
        summary = run(checkins.summary(checkin_id))
        assert summary["kind"] == "event"
        assert [(e["source"], e["player_id"], e["verdict"]) for e in summary["events"]] == \
            [("availability", BRUNSON, "out")]

    def test_what_can_no_longer_be_answered_is_dropped(self, team, night, on):
        tip = datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)
        _record(team, [_fact()])
        news = _item("rw-2")
        _record(team, [events.Fact(source=events.NEWS, player_id=BRUNSON, summary="old news", news_item_id=news.id)],
                now=NOW - timedelta(hours=13))
        assert run(events.drop_stale(team.team_id, NOW)) == 1                        # the news, half a day old
        assert run(events.drop_stale(team.team_id, tip + timedelta(minutes=1))) == 1  # the verdict, tipped
        reasons = sorted(e.reason for e in AutopilotEvent.select())
        assert reasons == ["stale", "tipped"]
        assert run(events.take(team.team_id, _claim(team), NOW)) == []

    def test_event_check_ins_are_outside_the_once_a_day_slot(self, team, night):
        first = _claim(team)
        run(checkins._settle(first, "done", None, None, 0, NOW))
        second = _claim(team)
        assert second is not None and second != first
        run(checkins._settle(second, "done", None, None, 0, NOW))
        assert run(events._event_checkins_on(team.team_id, DAY)) == 2
        # The scheduled kinds keep theirs
        assert _claim(team, kind="morning") is not None
        assert AiCheckin.select().where(AiCheckin.kind == "event").count() == 2


class TestFacts:
    def test_a_verdict_change_before_the_tip_is_a_fact_and_its_words_name_the_game(self, night):
        _verdict(BRUNSON, "out", "questionable", NOW - timedelta(minutes=3))
        found = run(events.facts([(BRUNSON, DAY)], [], NOW))
        assert [(f.player_id, f.verdict, f.game_id) for f in found] == [(BRUNSON, "out", GAME)]
        assert found[0].summary.startswith("Jalen Brunson ruled out for Tue Nov 10, BOS @ NYK (7:30 PM ET tip), "
                                           "per ESPN fantasy")
        assert found[0].tip_at == datetime(2026, 11, 11, 0, 30, tzinfo=timezone.utc)

    def test_no_change_a_questionable_verdict_a_tipped_game_or_no_game_is_no_fact(self, night):
        _verdict(BRUNSON, "out", "out", NOW)
        _verdict(HART, "questionable", "available", NOW)
        assert run(events.facts([(BRUNSON, DAY), (HART, DAY)], [], NOW)) == []
        PlayerAvailability.update(previous_verdict="questionable").where(PlayerAvailability.player == BRUNSON).execute()
        assert run(events.facts([(BRUNSON, DAY)], [], datetime(2026, 11, 11, 0, 31, tzinfo=timezone.utc))) == []
        Game.update(status="in_progress").where(Game.game_id == GAME).execute()
        assert run(events.facts([(BRUNSON, DAY)], [], NOW)) == []
        assert run(events.facts([(BRUNSON, DAY + timedelta(days=1))], [], NOW)) == []

    def test_a_fresh_news_item_of_an_event_kind_is_a_fact_per_player(self, night):
        fresh = _item("rw-3", players=(BRUNSON, HART))
        recap = _item("rw-4", kind="performance")
        old = _item("rw-5", published=NOW - timedelta(hours=4))
        found = run(events.facts([], [fresh.id, recap.id, old.id], NOW))
        assert sorted((f.player_id, f.news_item_id) for f in found) == sorted([(BRUNSON, fresh.id), (HART, fresh.id)])
        brunson = next(f for f in found if f.player_id == BRUNSON)
        assert brunson.summary == "Jalen Brunson: Brunson (knee) ruled out Tuesday (Rotowire, 5:00 PM ET)"


class TestNewsReads:
    def test_a_player_s_news_is_his_items_newest_first(self, night):
        older = _item("rw-6", published=NOW - timedelta(days=2))
        newer = _item("rw-7", headline="Brunson (knee) upgraded", published=NOW - timedelta(hours=1))
        _item("rw-8", players=(HART,), headline="Hart news")
        _item("rw-9", published=NOW - timedelta(days=9))
        items = run(news_service.player_news(BRUNSON, days=7, now=NOW))
        assert [i.id for i in items] == [newer.id, older.id]
        assert items[0].players == ((BRUNSON, "Jalen Brunson"),) and items[0].teams == ("NYK",)
        assert run(news_service.latest_item(BRUNSON, now=NOW)).id == newer.id
        assert run(news_service.latest_item(BRUNSON, now=NOW + timedelta(days=5))) is None

    def test_the_league_feed_filters_by_kind_team_roster_and_time(self, night):
        ruled_out = _item("rw-10")
        signed = _item("rg-1", kind="transaction", players=(HART,), teams=("BOS",), headline="Hart signs",
                       source="realgm_rss", published=NOW - timedelta(hours=2))
        _item("rg-2", kind="other", published=NOW - timedelta(hours=30))
        feed = run(news_service.news_feed(hours=24, now=NOW))
        assert [i.id for i in feed] == [ruled_out.id, signed.id]
        assert [i.id for i in run(news_service.news_feed(kinds=["transaction"], now=NOW))] == [signed.id]
        assert [i.id for i in run(news_service.news_feed(team="bos", now=NOW))] == [signed.id]
        assert [i.id for i in run(news_service.news_feed(player_ids=[BRUNSON], now=NOW))] == [ruled_out.id]
        assert run(news_service.news_feed(player_ids=[], now=NOW)) == []
        assert len(run(news_service.news_feed(hours=48, now=NOW))) == 3

    def test_the_verdict_is_today_s_game_or_the_next_one(self, night):
        _verdict(BRUNSON, "out", "questionable", NOW, day=DAY + timedelta(days=2), game_id=None)
        view = run(news_service.verdict_for(BRUNSON, DAY))
        assert (view["game_date"], view["verdict"], view["has_game"]) == ("2026-11-12", "out", False)
        _verdict(BRUNSON, "doubtful", None, NOW)
        assert run(news_service.verdict_for(BRUNSON, DAY))["verdict"] == "doubtful"
        assert run(news_service.verdict_for(HART, DAY)) is None


def _columns(table):
    return {r[0] for r in db.execute_sql(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'usr' AND table_name = %s",
        (table,)).fetchall()}


def test_0045_rollback_applies_and_the_forward_file_restores_it(team, night):
    forward = (Path(MIGRATIONS_DIR) / "0045__dime_events.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0045__dime_events.rollback.sql").read_text()
    event = _claim(team)
    morning = _claim(team, kind="morning", state="missed")
    try:
        db.execute_sql(rollback)
        assert not _columns("autopilot_events")
        kinds = {r[0] for r in db.execute_sql("SELECT kind FROM usr.ai_checkins").fetchall()}
        assert kinds == {"morning"} and event is not None and morning is not None
        with pytest.raises(Exception), db.atomic():
            db.execute_sql("INSERT INTO usr.ai_checkins (team_id, user_id, kind, nba_date, due_at, state, started_at) "
                           "VALUES (%s, %s, 'event', %s, now(), 'running', now())",
                           (team.team_id, team.user_id_id, DAY))
    finally:
        db.execute_sql(forward)
    assert "news_item_id" in _columns("autopilot_events")
    db.execute_sql(forward)                                                       # idempotent

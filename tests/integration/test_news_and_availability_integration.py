"""
Integration: migration 0034 (news items, availability, roster_moves reactions).

The models round-trip against the real tables; the generated `search` column
fills itself; one counted availability reaction per (team, day, trigger player)
is a database fact while the auto-lineup once-per-day index is untouched; and
the rollback applies and the forward file restores it.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from peewee import IntegrityError

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.nba import (
    AvailabilityReport,
    NBATeam,
    NewsItem,
    NewsItemPlayer,
    NewsItemTeam,
    Player,
    PlayerAvailability,
)
from db.models.roster_moves import RosterMove
from db.models.teams import Team
from db.models.users import User

pytestmark = [pytest.mark.integration]

DAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)
NEW_TABLES = ("news_items", "news_item_players", "news_item_teams", "availability_reports", "player_availability")


@pytest.fixture
def player(integration_db):
    return Player.create(id=1628983, espn_id=4278073, name="Shai Gilgeous-Alexander",
                         name_normalized="shai gilgeous-alexander",
                         created_at=datetime.utcnow(), updated_at=datetime.utcnow())


@pytest.fixture
def team(integration_db):
    user = User.create(email="avail@courtvision.dev", clerk_user_id="user_avail", created_at=datetime.utcnow())
    info = {"provider": "espn", "league_id": 552315826, "team_name": "Test", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="552315826Test", league_info=json.dumps(info))


def _reaction(team, status, trigger, source="availability", day=DAY):
    return RosterMove.create(user=team.user_id, team=team.team_id, nba_date=day, scoring_period_id=1,
                             source=source, status=status, trigger_player_id=trigger,
                             moves=[{"player_id": 1, "from_slot_id": 4, "to_slot_id": 12}])


def _tables() -> set[str]:
    rows = db.execute_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'nba' AND table_name = ANY(%s)",
        (list(NEW_TABLES),),
    ).fetchall()
    return {r[0] for r in rows}


def _trigger_column() -> bool:
    return db.execute_sql(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'usr' "
        "AND table_name = 'roster_moves' AND column_name = 'trigger_player_id'"
    ).fetchone() is not None


def test_news_item_round_trip_with_links_and_search(player):
    team, _ = NBATeam.get_or_create(id="OKC", defaults={
        "name": "Oklahoma City Thunder", "conference": "West", "division": "Northwest",
        "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()})
    item = NewsItem.create(source="espn_player_news", source_item_id="rw-1",
                           published_at=NOW, headline="Gilgeous-Alexander ruled out Tuesday",
                           body="He will miss the game with a left knee sprain.", kind="availability",
                           raw={"id": "rw-1"})
    NewsItemPlayer.create(news_item=item, player=player, linked_by="espn_id")
    NewsItemTeam.create(news_item=item, team=team)

    hits = db.execute_sql(
        "SELECT id FROM nba.news_items WHERE search @@ plainto_tsquery('english', 'knee sprain')"
    ).fetchall()
    assert hits == [(item.id,)]
    assert [link.news_item_id for link in player.news_links] == [item.id]
    with pytest.raises(IntegrityError), db.atomic():
        NewsItem.create(source="espn_player_news", source_item_id="rw-1", headline="dup")
    with pytest.raises(IntegrityError), db.atomic():
        NewsItem.create(source="x", source_item_id="k", headline="h", kind="gossip")


def test_availability_report_and_verdict_round_trip(player):
    report = AvailabilityReport.create(player=player, game_id="0022600003", game_date=DAY,
                                       source="nba_report", status="out", reason="Left Knee",
                                       detail="Injury/Illness - Left Knee; Sprain",
                                       source_time=NOW, confidence=90)
    PlayerAvailability.create(player=player, game_date=DAY, game_id="0022600003", verdict="out",
                              decided_by="nba_report", reason="Left Knee", confidence=90,
                              previous_verdict="questionable", changed_at=NOW)
    verdict = PlayerAvailability.get(PlayerAvailability.player == player, PlayerAvailability.game_date == DAY)
    assert (verdict.verdict, verdict.final, verdict.previous_verdict) == ("out", False, "questionable")
    assert AvailabilityReport.get_by_id(report.id).confidence == 90
    with pytest.raises(IntegrityError), db.atomic():   # one verdict per player per game date
        PlayerAvailability.create(player=player, game_date=DAY, verdict="available")
    with pytest.raises(IntegrityError), db.atomic():
        AvailabilityReport.create(player=player, game_date=DAY, source="twitter", status="out", confidence=50)
    with pytest.raises(IntegrityError), db.atomic():
        AvailabilityReport.create(player=player, game_date=DAY, source="nba_report", status="gtd", confidence=50)


def test_one_counted_reaction_per_scratch_per_team_per_day(team):
    _reaction(team, "failed", trigger=1628983)      # a refused attempt does not count
    _reaction(team, "applied", trigger=1628983)
    with pytest.raises(IntegrityError), db.atomic():
        _reaction(team, "noop", trigger=1628983)
    _reaction(team, "applied", trigger=203999)      # a second scratch the same night gets its own write
    _reaction(team, "applied", trigger=1628983, day=date(2026, 10, 21))
    _reaction(team, "applied", trigger=None, source="auto")   # the morning pass is its own index
    assert RosterMove.select().where(RosterMove.source == "availability").count() == 4


def test_the_source_check_refuses_an_unknown_source(team):
    with pytest.raises(IntegrityError), db.atomic():
        _reaction(team, "applied", trigger=1, source="news")


def test_0034_rollback_applies_and_the_forward_file_restores_it(integration_db):
    forward = (Path(MIGRATIONS_DIR) / "0034__news_and_availability.sql").read_text()
    rollback = (Path(MIGRATIONS_DIR) / "0034__news_and_availability.rollback.sql").read_text()
    assert _tables() == set(NEW_TABLES) and _trigger_column()
    try:
        with db.atomic():
            db.execute_sql(rollback)
        assert _tables() == set() and not _trigger_column()
    finally:
        with db.atomic():
            db.execute_sql(forward)                 # the chain's state is untouched
    assert _tables() == set(NEW_TABLES) and _trigger_column()
    with db.atomic():
        db.execute_sql(forward)                     # idempotent: a second apply is a no-op

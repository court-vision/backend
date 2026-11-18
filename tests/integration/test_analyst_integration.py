"""
The analyst in shadow against the real schema (migration 0053): its notes in a
scope of their own beside the notes written by hand, the record of every read,
and its spend line, which belongs to no user.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from peewee import IntegrityError

from core.settings import settings
from db.base import db
from db.models.ai_conversations import AiSpend
from db.models.nba import AnalystRead, Player, ProjectionAdjustment
from db.models.nba.analyst_reads import SETTLED
from db.models.users import User
from services.ai import spend

pytestmark = pytest.mark.integration

SEASON = "2026-27"
DAY = date(2026, 11, 16)


def run(coro):
    return asyncio.run(coro)


def _player(pid=1):
    return Player.create(id=pid, name=f"P{pid}", name_normalized=f"p{pid}")


def _note(pid=1, scope="analyst", **fields):
    fields = {"kind": "injury_current", "return_date": date(2026, 12, 8), "review_date": date(2026, 11, 30),
              "note": "re-evaluated in two weeks", "author": scope, **fields}
    return ProjectionAdjustment.record(pid, SEASON, scope=scope, **fields)


def _read(pid=1, outcome="none", fingerprint="a" * 64, at=None, day=DAY, **fields):
    return AnalystRead.create(season=SEASON, as_of_date=day, player=pid, reasons=["a new injury item"],
                              fingerprint=fingerprint, outcome=outcome,
                              created_at=at or datetime.now(timezone.utc), **fields)


class TestTheAnalystsNotes:
    def test_its_note_sits_beside_the_note_written_by_hand_and_the_preseason_adjustment(self):
        _player()
        before = ProjectionAdjustment.record(1, SEASON, kind="year2", usage=1.05, note="second-year jump", author="t")
        mine = _note(scope="season", return_date=date(2026, 12, 1), author="james")
        its = _note(return_earliest=date(2026, 12, 1), return_latest=date(2026, 12, 20), confidence="likely",
                    evidence=[{"type": "news", "id": 91, "headline": "Re-evaluated in two weeks"}])

        # Three live rows for one player, one a scope, and each reader gets its own
        assert [a.id for a in ProjectionAdjustment.active_for(SEASON)] == [before.id]
        assert [a.id for a in ProjectionAdjustment.active_for(SEASON, "season")] == [mine.id]
        assert [a.id for a in ProjectionAdjustment.active_for(SEASON, "analyst")] == [its.id]
        stored = ProjectionAdjustment.get_by_id(its.id)
        assert (stored.scope, stored.author, stored.confidence) == ("analyst", "analyst", "likely")
        assert stored.evidence == [{"type": "news", "id": 91, "headline": "Re-evaluated in two weeks"}]

    def test_a_new_read_supersedes_its_own_note_and_never_the_owners(self):
        _player()
        mine = _note(scope="season", author="james")
        first = _note()
        second = _note(return_date=date(2026, 12, 15))

        assert ProjectionAdjustment.get_by_id(first.id).superseded_by_id == second.id
        assert ProjectionAdjustment.get_by_id(mine.id).superseded_by_id is None
        assert ProjectionAdjustment.retire(1, SEASON, "analyst") == 1
        assert ProjectionAdjustment.active_for(SEASON, "analyst") == []
        assert [a.id for a in ProjectionAdjustment.active_for(SEASON, "season")] == [mine.id]

    @pytest.mark.parametrize("fields", [
        dict(kind="role", minutes=30),                                          # no day to read it again
        dict(kind="role", rates={"blk": 1.1}, review_date=date(2026, 12, 1)),   # a note moves inputs, not rates
        dict(kind="role", minutes=30, review_date=date(2026, 12, 1), confidence="certain"),
    ])
    def test_it_is_held_to_what_a_note_in_season_is_held_to(self, fields):
        _player()
        with pytest.raises(IntegrityError), db.atomic():
            ProjectionAdjustment.insert(player=1, season=SEASON, scope="analyst", note="x", author="analyst",
                                        **fields).execute()

    def test_a_second_live_note_of_its_own_is_refused(self):
        _player()
        _note()
        with pytest.raises(IntegrityError), db.atomic():
            ProjectionAdjustment.insert(player=1, season=SEASON, scope="analyst", kind="role", minutes=30,
                                        review_date=date(2026, 12, 1), note="y", author="analyst").execute()


class TestTheRecordOfItsReads:
    def test_a_read_keeps_what_came_back_and_the_note_it_left(self):
        _player()
        note = _note()
        proposal = {"decision": "write", "return_date": "2026-12-08", "cites": ["news:91"]}
        read = _read(outcome="note", summary="Out until early December", note=note.id, proposal=proposal,
                     disagrees="The note written by hand says December 1", model="claude-opus-5-5",
                     prompt_version="analyst-1", input_tokens=2_000, output_tokens=500, cost_micros=18_200)

        stored = AnalystRead.get_by_id(read.id)
        assert (stored.outcome, stored.note_id, stored.cost_micros) == ("note", note.id, 18_200)
        assert stored.reasons == ["a new injury item"] and stored.proposal == proposal
        # The read outlives the note it left
        ProjectionAdjustment.delete().where(ProjectionAdjustment.id == note.id).execute()
        assert AnalystRead.get_by_id(read.id).note_id is None

    def test_an_outcome_it_does_not_know_is_refused(self):
        _player()
        with pytest.raises(IntegrityError), db.atomic():
            _read(outcome="guessed")

    def test_the_fingerprint_that_counts_is_the_newest_read_that_came_to_an_answer(self):
        _player()
        _player(2)
        _player(3)
        now = datetime.now(timezone.utc)
        _read(1, "note", "1" * 64, at=now - timedelta(days=2), day=DAY - timedelta(days=2))
        _read(1, "unchanged", "2" * 64, at=now - timedelta(days=1), day=DAY - timedelta(days=1))
        # A read that failed or was declined is read again: it settles nothing
        _read(1, "failed", "3" * 64, at=now)
        _read(2, "declined", "4" * 64, at=now)
        _read(3, "refused", "5" * 64, at=now)
        # Another season's reads are not this one's
        AnalystRead.create(season="2025-26", as_of_date=DAY, player=2, reasons=[], fingerprint="6" * 64,
                           outcome="none")

        assert AnalystRead.settled_fingerprints(SEASON, [1, 2, 3]) == {1: "2" * 64, 3: "5" * 64}
        assert AnalystRead.settled_fingerprints(SEASON, []) == {}
        assert set(SETTLED) == {"note", "unchanged", "none", "retired", "refused"}

    def test_a_mornings_reads_newest_first(self):
        _player()
        _player(2)
        now = datetime.now(timezone.utc)
        first = _read(1, at=now - timedelta(minutes=2))
        second = _read(2, "note", at=now - timedelta(minutes=1))
        _read(1, day=DAY - timedelta(days=1))

        assert [r.id for r in AnalystRead.of_day(SEASON, DAY)] == [second.id, first.id]

    def test_a_players_reads_go_with_him(self):
        _player()
        _read()
        Player.delete().where(Player.id == 1).execute()
        assert AnalystRead.select().count() == 0


class TestItsSpendLine:
    @pytest.fixture(autouse=True)
    def budgets(self, monkeypatch):
        monkeypatch.setattr(settings, "analyst_monthly_budget_usd", 0.75)
        monkeypatch.setattr(settings, "analyst_read_max_usd", 0.30)

    def test_reads_reserve_on_a_line_that_belongs_to_no_user(self):
        first = run(spend.reserve_analyst())
        second = run(spend.reserve_analyst())

        row = AiSpend.get_by_id(first)
        assert (row.line, row.user_id, row.cost_micros, row.settled) == ("analyst", None, 300_000, False)
        # A third ceiling would pass 0.75: refused, not raised
        assert second and run(spend.reserve_analyst()) is None
        assert run(spend.analyst_month_spend()) == (600_000, 750_000)

        # Settled to what the read cost, there is room again
        run(spend.settle(first, 18_200))
        assert run(spend.analyst_month_spend()) == (318_200, 750_000)
        assert run(spend.reserve_analyst()) is not None

    def test_a_read_that_never_ran_hands_its_reservation_back(self):
        reservation = run(spend.reserve_analyst())
        run(spend.cancel(reservation))
        assert run(spend.analyst_month_spend()) == (0, 750_000)

    def test_a_read_that_never_answered_stays_charged_at_its_ceiling(self):
        reservation = run(spend.reserve_analyst())
        run(spend.hold(reservation))

        row = AiSpend.get_by_id(reservation)
        assert (row.cost_micros, row.settled) == (300_000, True)
        assert run(spend.analyst_month_spend()) == (300_000, 750_000)
        # Final: a later cancel does not hand it back
        run(spend.cancel(reservation))
        assert run(spend.analyst_month_spend()) == (300_000, 750_000)

    def test_the_desks_and_the_check_ins_caps_do_not_see_it(self, monkeypatch):
        me = User.create(email="a1@courtvision.dev", clerk_user_id="user_a1", created_at=datetime.now(timezone.utc))
        monkeypatch.setattr(settings, "ai_agent_user_ids", [me.user_id])
        monkeypatch.setattr(settings, "ai_monthly_budget_usd", 10.0)
        monkeypatch.setattr(settings, "ai_agent_turn_max_usd", 1.0)
        monkeypatch.setattr(settings, "autopilot_monthly_budget_usd", 2.5)
        run(spend.reserve_analyst())
        run(spend.reserve_analyst())

        assert run(spend.month_spend(me.user_id)).total == 0
        assert run(spend.autopilot_month_spend()) == (0, 2_500_000)
        # And the other way round: a desk turn and a check-in leave the analyst's month alone
        run(spend.reserve(me.user_id))
        run(spend.reserve_autopilot(me.user_id))
        assert run(spend.analyst_month_spend()) == (600_000, 750_000)

    def test_only_the_analysts_line_may_go_without_a_user(self):
        for line in ("desk", "autopilot"):
            with pytest.raises(IntegrityError), db.atomic():
                AiSpend.insert(user_id=None, cost_micros=1, line=line).execute()
        with pytest.raises(IntegrityError), db.atomic():
            AiSpend.insert(user_id=None, cost_micros=1, line="other").execute()

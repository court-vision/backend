"""The pure half of the INIT sync service: header derivation, pick
classification, keeper handling, and the plan_keeper_moves by_me filter."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from schemas.draft import DraftSyncConflict
from services.draft_service import plan_keeper_moves
from services.draft_sync_service import classify_pick, derive_header, header_warnings
from utils.espn_draft_init import decode_init, strip_init_prefix

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _decode(name: str) -> dict:
    return decode_init(strip_init_prefix((FIXTURES / name).read_text()))


def _pick(overall, *, player_id=None, espn=None, name=None):
    return SimpleNamespace(
        overall_pick=overall, player_id=player_id, espn_player_id=espn, player_name=name
    )


@pytest.mark.unit
def test_derive_header_room_open():
    h = derive_header(_decode("espn_draft_init_roomopen.b64"))
    assert h.espn_league_id == 35392660
    assert h.espn_team_id == 5
    assert h.pick_order == [1, 2, 3, 4, 5, 6, 7, 8]
    assert h.my_slot == 5
    assert h.rounds == 13
    assert h.draft_type == "snake"
    assert h.espn_front == 1
    assert h.draft_state == 0


@pytest.mark.unit
def test_derive_header_in_progress():
    h = derive_header(_decode("espn_draft_init_inprogress_45picks.b64"))
    assert h.espn_team_id == 4
    assert h.my_slot == 4
    assert h.espn_front == 46


@pytest.mark.unit
def test_classify_skip_when_same_number_same_player():
    existing = [_pick(1, espn=100)]
    assert classify_pick(existing, 1, 100, None, None) == "skip"


@pytest.mark.unit
def test_classify_number_taken_by_a_different_player():
    existing = [_pick(1, espn=999)]
    v = classify_pick(existing, 1, 100, None, None)
    assert isinstance(v, DraftSyncConflict)
    assert v.reason == "pick_number_taken"
    assert v.held_espn_player_id == 999


@pytest.mark.unit
def test_classify_player_already_drafted_elsewhere():
    # By ESPN id, by NBA id, and by name-only (a lagging pre-sync row).
    assert classify_pick([_pick(7, espn=100)], 3, 100, None, None).reason == "player_already_drafted"
    assert classify_pick([_pick(7, player_id=55)], 3, 100, 55, None).reason == "player_already_drafted"
    by_name = classify_pick([_pick(7, name="Nikola Jokic")], 3, 100, None, "Nikola Jokic")
    assert by_name.reason == "player_already_drafted"
    assert by_name.held_at == 7


@pytest.mark.unit
def test_classify_insert_when_free():
    assert classify_pick([_pick(1, espn=1)], 2, 100, None, None) == "insert"
    assert classify_pick([], 1, 100, None, None) == "insert"


@pytest.mark.unit
def test_header_warnings_lists_each_difference():
    h = derive_header(_decode("espn_draft_init_roomopen.b64"))  # order [1..8], slot 5, rounds 13, snake
    session = SimpleNamespace(pick_order=[8, 7, 6, 5, 4, 3, 2, 1], my_slot=3, rounds=12, draft_type="auction")
    w = header_warnings(h, session)
    assert any("pick order" in x for x in w)
    assert any("slot" in x for x in w)
    assert any("rounds" in x for x in w)
    assert any("draft type" in x for x in w)


@pytest.mark.unit
def test_header_warnings_silent_when_matching():
    h = derive_header(_decode("espn_draft_init_roomopen.b64"))
    session = SimpleNamespace(pick_order=[1, 2, 3, 4, 5, 6, 7, 8], my_slot=5, rounds=13, draft_type="snake")
    assert header_warnings(h, session) == []


@pytest.mark.unit
def test_plan_keeper_moves_ignores_other_seats_keepers():
    # A synced keeper from another seat (by_me=False) has no designation and must
    # not be repriced — plan_keeper_moves should skip it rather than raise.
    keepers = []  # my designated keepers (none)
    picks = [SimpleNamespace(overall_pick=8, source="keeper", by_me=False, player_id=1, espn_player_id=None, player_name=None)]
    assert plan_keeper_moves(keepers, picks) == {}

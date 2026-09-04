"""The INIT decoder and its derived views, against two fixture payloads."""

from pathlib import Path

import pytest

from utils.espn_draft_init import (
    decode_init,
    draft_type_of,
    espn_front,
    made_picks,
    my_slot_of,
    pick_order_of,
    position_limits_of,
    rounds_of,
    strip_init_prefix,
    team_count,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _decode(name: str) -> dict:
    return decode_init(strip_init_prefix((FIXTURES / name).read_text()))


@pytest.fixture
def roomopen() -> dict:
    return _decode("espn_draft_init_roomopen.b64")


@pytest.fixture
def inprogress() -> dict:
    return _decode("espn_draft_init_inprogress_45picks.b64")


@pytest.mark.unit
def test_both_fixtures_decode_byte_exact(roomopen, inprogress):
    for d in (roomopen, inprogress):
        assert d["_bytes_consumed"] == d["_bytes_total"]


@pytest.mark.unit
def test_room_open_header(roomopen):
    assert (roomopen["leagueId"], roomopen["teamId"]) == (35392660, 5)
    assert team_count(roomopen) == 8
    assert len(pick_order_of(roomopen)) == 8
    assert pick_order_of(roomopen) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert my_slot_of(roomopen) == 5
    assert rounds_of(roomopen) == 13
    assert draft_type_of(roomopen) == "snake"
    assert made_picks(roomopen) == []
    assert espn_front(roomopen) == 1
    assert position_limits_of(roomopen) == {"C": 4}


@pytest.mark.unit
def test_in_progress_header_and_picks(inprogress):
    assert (inprogress["leagueId"], inprogress["teamId"]) == (588175580, 4)
    assert my_slot_of(inprogress) == 4
    assert rounds_of(inprogress) == 13
    made = made_picks(inprogress)
    assert len(made) == 45
    assert espn_front(inprogress) == 46
    first = made[0]
    assert (first["pickNumber"], first["teamId"], first["playerId"], first["slotId"]) == (1, 1, 3112335, 5)
    assert first["isKeeper"] is False
    # by_me is derived downstream from teamId == the connecting team (4).
    assert any(p["teamId"] == 4 for p in made)


@pytest.mark.unit
def test_strip_prefix_tolerates_the_init_token():
    assert strip_init_prefix("INIT AAAA==") == "AAAA=="
    assert strip_init_prefix("  AAAA==  ") == "AAAA=="


@pytest.mark.unit
def test_garbage_and_truncation_raise():
    with pytest.raises(Exception):
        decode_init("bm90IGEgZHJhZnQgcGF5bG9hZA==")  # valid base64, wrong bytes
    with pytest.raises(Exception):
        decode_init("AAAAAQ==")  # truncated mid-stream

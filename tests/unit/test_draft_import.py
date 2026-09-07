"""Reading a completed ESPN draft: the header, the picks, and whose they are.

The payload is the real one — `tests/fixtures/espn_draft_detail_4team.json`,
the same draft `draft_replay_4team_13round.json` replays — so these assert what
ESPN sent, not what we imagine it sends.
"""

import copy
import json
from pathlib import Path

import pytest

from core.errors import BadRequestError
from services.draft_import_service import header_from_detail, made_picks, my_team_id

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DETAIL = json.loads((FIXTURES / "espn_draft_detail_4team.json").read_text())
MY_TEAM = "Replay Team Four"      # seat 3 of the captured pick order


@pytest.fixture
def payload():
    return copy.deepcopy(DETAIL)


def test_the_capture_is_a_finished_draft(payload):
    """Guards the fixture itself, the way the replay capture is guarded."""
    detail = payload["draftDetail"]
    assert detail["drafted"] is True and detail["inProgress"] is False
    assert len(detail["picks"]) == 52
    assert sorted(p["overallPickNumber"] for p in detail["picks"]) == list(range(1, 53))
    # The SWID of whoever clicked each human pick, scrubbed on the way in.
    assert "memberId" not in json.dumps(payload["draftDetail"])


def test_the_header_comes_off_the_drafts_own_settings(payload):
    header = header_from_detail(payload, espn_team_id=4)

    assert header.espn_league_id == 552315826
    assert header.pick_order == [2, 1, 4, 3]
    assert header.draft_type == "snake"
    assert header.rounds == 13
    assert header.my_slot == 3            # team 4 sits third in the order
    assert header.espn_front == 53
    assert header.draft_state == 2        # ESPN's "after the draft"


def test_the_first_round_stands_in_when_a_payload_has_no_pick_order(payload):
    """Older payloads carry the order only in the picks themselves."""
    payload["settings"]["draftSettings"].pop("pickOrder")

    header = header_from_detail(payload, espn_team_id=4)

    assert header.pick_order == [2, 1, 4, 3]
    assert header.my_slot == 3


def test_a_team_that_never_picked_has_no_slot(payload):
    assert header_from_detail(payload, espn_team_id=99).my_slot is None


def test_every_pick_is_read_in_order_with_its_team(payload):
    picks = list(made_picks(payload["draftDetail"]))

    assert [p.pick_number for p in picks] == list(range(1, 53))
    assert picks[0].espn_player_id == 5104157 and picks[0].espn_team_id == 2
    assert not any(p.is_keeper for p in picks)      # keeperCount was 0
    assert all(p.bid_amount == 0 for p in picks)    # a snake, so nothing is bid


def test_an_empty_slot_is_not_a_pick(payload):
    """`playerId: -1` is ESPN's pre-draft sentinel, not a player."""
    payload["draftDetail"]["picks"][0]["playerId"] = -1

    picks = list(made_picks(payload["draftDetail"]))

    assert len(picks) == 51 and picks[0].pick_number == 2
    assert header_from_detail(payload, espn_team_id=4).rounds == 13


def test_the_caller_is_the_team_whose_name_matches(payload):
    assert my_team_id(payload, MY_TEAM) == 4
    assert my_team_id(payload, f"  {MY_TEAM}  ") == 4


def test_a_name_that_is_not_in_the_league_is_a_400_that_lists_the_teams(payload):
    with pytest.raises(BadRequestError) as exc:
        my_team_id(payload, "Somebody Else")

    assert exc.value.error_code == "TEAM_NAME_NOT_IN_LEAGUE"
    assert MY_TEAM in exc.value.message

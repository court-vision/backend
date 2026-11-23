"""
/v1/internal/teams/{id}/holds over the test app (the Dime autopilot plan, step 4): the list,
applying a steer chip, clearing a hold. Ownership via the usual `ensure_team_owned` stub; the
holds' rows and the chip's apply are faked at the service boundary, so the routes' plumbing,
the shape the Week desk reads and every documented status are exercised.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.errors import ConflictError
from core.settings import settings
from services import steering
from services.ai import steering as ai_steering

AT = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)


def hold_row(**kw):
    base = dict(id=5, kind="override", player_id=4594268, nba_player_id=1630162, player_name="Anthony Edwards",
                player_team="MIN", share=0.0, starts_on=date(2026, 11, 16), ends_on=date(2026, 11, 17),
                reason="Out with a hamstring through Tuesday", applied_by="user", proposal_id_id=41, created_at=AT)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def owned(monkeypatch):
    from api import deps
    from services import user_sync_service
    user = MagicMock(); user.user_id = 42
    monkeypatch.setattr(user_sync_service.UserSyncService, "get_or_create_user", staticmethod(lambda c, e: user))
    monkeypatch.setattr(deps, "ensure_team_owned", lambda team_id, user_id: SimpleNamespace(team_id=7, user_id_id=42))


@pytest.fixture
def rows(monkeypatch):
    held = {"rows": [hold_row()]}

    async def live_rows(team_id, today=None):
        assert team_id == 7
        return list(held["rows"])
    monkeypatch.setattr(steering, "live_rows", live_rows)
    return held


@pytest.fixture
def on_the_ai_desk(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_agent_user_ids", [42])


@pytest.mark.api
@pytest.mark.parametrize("ai_enabled, desk_users", [(True, []), (True, [7]), (False, [42])])
def test_an_account_dime_cant_steer_for_still_sees_its_holds(authed_client, owned, rows, monkeypatch, ai_enabled,
                                                             desk_users):
    # No new hold can come (so the Week desk stops polling), but one already set still steers and lists
    monkeypatch.setattr(settings, "ai_enabled", ai_enabled)
    monkeypatch.setattr(settings, "ai_agent_user_ids", desk_users)
    r = authed_client.get("/v1/internal/teams/7/holds")
    data = r.json()["data"]
    assert r.status_code == 200 and data["enabled"] is False
    assert [h["hold_id"] for h in data["holds"]] == [5]


@pytest.mark.api
def test_the_list_says_what_each_hold_does_and_why(authed_client, owned, rows, on_the_ai_desk):
    r = authed_client.get("/v1/internal/teams/7/holds")
    data = r.json()["data"]
    assert r.status_code == 200 and data["enabled"] is True
    (h,) = data["holds"]
    assert (h["hold_id"], h["kind"], h["share"], h["starts_on"], h["ends_on"], h["proposal_id"]) == \
        (5, "override", 0.0, "2026-11-16", "2026-11-17", 41)
    assert h["player"] == {"espn_id": 4594268, "nba_player_id": 1630162, "name": "Anthony Edwards", "team": "MIN"}
    assert h["effect"].startswith("Count Anthony Edwards out") and h["reason"].startswith("Out with a hamstring")


@pytest.mark.api
def test_applying_a_steer_chip_returns_the_holds_then(authed_client, owned, rows, monkeypatch, on_the_ai_desk):
    asked = []

    async def apply(team, proposal_id, now=None):
        asked.append((team.team_id, team.user_id, proposal_id))
        rows["rows"].append(hold_row(id=6, kind="keep", share=None))
        return 1
    monkeypatch.setattr(ai_steering, "apply_holds", apply)
    r = authed_client.post("/v1/internal/teams/7/holds", json={"proposal_id": 41})
    assert r.status_code == 200 and asked == [(7, 42, 41)]
    assert r.json()["message"] == "1 hold(s) set" and [h["hold_id"] for h in r.json()["data"]["holds"]] == [5, 6]


@pytest.mark.api
def test_a_chip_that_cant_be_applied_is_refused(authed_client, owned, rows, monkeypatch):
    async def apply(team, proposal_id, now=None):
        raise ConflictError("AI_PROPOSAL_NOT_PENDING", "Already applied")
    monkeypatch.setattr(ai_steering, "apply_holds", apply)
    r = authed_client.post("/v1/internal/teams/7/holds", json={"proposal_id": 41})
    assert r.status_code == 409 and r.json()["error_code"] == "AI_PROPOSAL_NOT_PENDING"


@pytest.mark.api
def test_a_bad_body_is_fastapis_422(authed_client, owned):
    assert authed_client.post("/v1/internal/teams/7/holds", json={}).status_code == 422
    assert authed_client.post("/v1/internal/teams/7/holds", json={"proposal_id": 0}).status_code == 422


@pytest.mark.api
def test_clearing_a_hold(authed_client, owned, rows, monkeypatch):
    cleared = []

    async def clear(team_id, hold_id, *, by="user", now=None):
        cleared.append((team_id, hold_id, by))
        if hold_id != 5:
            return None
        rows["rows"] = []
        return hold_row()
    monkeypatch.setattr(steering, "clear", clear)
    r = authed_client.delete("/v1/internal/teams/7/holds/5")
    assert r.status_code == 200 and r.json()["data"]["holds"] == [] and cleared == [(7, 5, "user")]
    assert r.json()["message"] == "Hold on Anthony Edwards cleared"
    r = authed_client.delete("/v1/internal/teams/7/holds/9")
    assert r.status_code == 404 and r.json()["error_code"] == "HOLD_NOT_FOUND"

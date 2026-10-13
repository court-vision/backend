"""
UserSyncService against Postgres: the email is fetched from Clerk only when a
user has to be created or linked (see tests/unit/test_user_sync_email.py).
"""

import pytest

from db.models.users import User
from services import user_sync_service
from services.user_sync_service import UserSyncService


@pytest.fixture
def clerk(monkeypatch):
    calls = []

    def fetch(clerk_user_id):
        calls.append(clerk_user_id)
        return {"clerk_user_id": clerk_user_id, "email": "fetched@courtvision.dev"}
    monkeypatch.setattr(user_sync_service.clerk_auth, "fetch_clerk_user", fetch)
    return calls


@pytest.mark.integration
def test_returning_user_without_email_claim_costs_no_clerk_call(integration_db, clerk):
    existing = User.create(clerk_user_id="user_back", email="back@courtvision.dev")
    user = UserSyncService.get_or_create_user("user_back", None)
    assert user.user_id == existing.user_id
    assert clerk == []


@pytest.mark.integration
def test_first_sign_in_without_email_claim_creates_the_user(integration_db, clerk):
    user = UserSyncService.get_or_create_user("user_first", None)
    assert clerk == ["user_first"]
    row = User.get(User.clerk_user_id == "user_first")
    assert (row.user_id, row.email) == (user.user_id, "fetched@courtvision.dev")


@pytest.mark.integration
def test_pre_clerk_user_is_linked_by_the_fetched_email(integration_db, clerk):
    legacy = User.create(clerk_user_id=None, email="fetched@courtvision.dev")
    user = UserSyncService.get_or_create_user("user_linked", None)
    assert user.user_id == legacy.user_id
    assert User.get_by_id(legacy.user_id).clerk_user_id == "user_linked"
    assert User.select().count() == 1


@pytest.mark.integration
def test_email_claim_still_updates_a_changed_email(integration_db, clerk):
    User.create(clerk_user_id="user_moved", email="old@courtvision.dev")
    UserSyncService.get_or_create_user("user_moved", "new@courtvision.dev")
    assert User.get(User.clerk_user_id == "user_moved").email == "new@courtvision.dev"
    assert clerk == []

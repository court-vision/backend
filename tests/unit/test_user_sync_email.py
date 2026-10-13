"""
When UserSyncService asks Clerk for a user's email.

Token verification used to fetch the email from Clerk's Backend API on every
request whose token had no email claim, about 100 ms each (300-480 ms in a
desk-open burst). Only creating or linking a user needs it, so that is now the
one place it is fetched: a user who already has a row costs no Clerk call,
claim or not. The real-database version of these is
tests/integration/test_user_sync_integration.py.
"""

import pytest

from core.errors import ServiceUnavailableError
from services import user_sync_service
from services.user_sync_service import UserSyncService


class _Field:
    """`User.<field> == value` as a (field, value) pair the fake query matches on."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return (self.name, value)

    __hash__ = object.__hash__


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def where(self, condition):
        field, value = condition
        return _Query([r for r in self.rows if getattr(r, field) == value])

    def first(self):
        return self.rows[0] if self.rows else None


class FakeUser:
    clerk_user_id = _Field("clerk_user_id")
    email = _Field("email")
    rows: list = []
    saves = 0

    def __init__(self, clerk_user_id=None, email=None, **kwargs):
        self.clerk_user_id = clerk_user_id
        self.email = email

    @classmethod
    def select(cls):
        return _Query(cls.rows)

    @classmethod
    def create(cls, **kwargs):
        row = cls(**kwargs)
        cls.rows.append(row)
        return row

    def save(self):
        type(self).saves += 1


@pytest.fixture
def users(monkeypatch):
    class Users(FakeUser):
        rows = []
        saves = 0
    monkeypatch.setattr(user_sync_service, "User", Users)
    return Users


@pytest.fixture
def clerk(monkeypatch):
    """Clerk's Backend API, answering with `email` (None: the fetch failed)."""
    state = {"email": "fetched@example.com", "calls": []}

    def fetch(clerk_user_id):
        state["calls"].append(clerk_user_id)
        return None if state["email"] is None else {"clerk_user_id": clerk_user_id, "email": state["email"]}
    monkeypatch.setattr(user_sync_service.clerk_auth, "fetch_clerk_user", fetch)
    return state


@pytest.mark.unit
def test_existing_user_without_email_claim_costs_no_clerk_call(users, clerk):
    users.rows.append(users(clerk_user_id="user_1", email="me@example.com"))
    user = UserSyncService.get_or_create_user("user_1", None)
    assert user.email == "me@example.com"
    assert clerk["calls"] == []
    assert users.saves == 0


@pytest.mark.unit
def test_existing_user_email_follows_the_claim(users, clerk):
    users.rows.append(users(clerk_user_id="user_1", email="old@example.com"))
    user = UserSyncService.get_or_create_user("user_1", "new@example.com")
    assert user.email == "new@example.com"
    assert users.saves == 1
    assert clerk["calls"] == []


@pytest.mark.unit
def test_new_user_without_email_claim_is_created_with_the_fetched_email(users, clerk):
    user = UserSyncService.get_or_create_user("user_new", None)
    assert clerk["calls"] == ["user_new"]
    assert (user.clerk_user_id, user.email) == ("user_new", "fetched@example.com")
    assert users.rows == [user]


@pytest.mark.unit
def test_new_user_with_email_claim_is_created_without_a_clerk_call(users, clerk):
    user = UserSyncService.get_or_create_user("user_new", "claim@example.com")
    assert clerk["calls"] == []
    assert user.email == "claim@example.com"


@pytest.mark.unit
def test_pre_clerk_user_is_linked_by_the_fetched_email(users, clerk):
    legacy = users(clerk_user_id=None, email="fetched@example.com")
    users.rows.append(legacy)
    user = UserSyncService.get_or_create_user("user_new", None)
    assert user is legacy
    assert legacy.clerk_user_id == "user_new"
    assert clerk["calls"] == ["user_new"]
    assert len(users.rows) == 1


@pytest.mark.unit
def test_new_user_whose_email_cannot_be_fetched_is_503(users, clerk):
    clerk["email"] = None
    with pytest.raises(ServiceUnavailableError) as exc:
        UserSyncService.get_or_create_user("user_new", None)
    assert exc.value.error_code == "USER_EMAIL_UNAVAILABLE"
    assert users.rows == []

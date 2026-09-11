"""
Integration: ESPN connections against the real schema.

Migration 0024's normalize-and-merge of existing rows, the one-row-per-account
upsert that makes a cookie refresh reach every team, the view the client sees,
and owner-scoped deletion.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from core import crypto
from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.provider_connections import ProviderConnection
from db.models.teams import Team
from db.models.users import User
from schemas.common import FantasyProvider, LeagueInfo
from services import connection_service, credential_service
from services.team_service import TeamService

pytestmark = [pytest.mark.integration]

SWID = "{3F2A9C1E-1B2C-4D5E-8F90-A1B2C3D4E5F6}"
OTHER = "{00000000-AAAA-BBBB-CCCC-DDDDDDDDDDDD}"
FORWARD = Path(MIGRATIONS_DIR) / "0024__espn_connection_keys_and_status.sql"
ROLLBACK = Path(MIGRATIONS_DIR) / "0024__espn_connection_keys_and_status.rollback.sql"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    from core.settings import settings

    monkeypatch.setattr(settings, "credential_keys", f"1:{Fernet.generate_key().decode()}", raising=False)
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest.fixture
def user(integration_db):
    return User.create(email="espn@courtvision.dev", clerk_user_id="user_espn", created_at=datetime.now(timezone.utc))


def _team(user, name, connection_id=None):
    info = {"provider": "espn", "league_id": 1234, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"1234{name}", league_info=json.dumps(info),
                       provider_connection=connection_id)


def _payload(team, espn_s2, swid):
    """What TeamService hands persist(): the team's league_info plus the cookies."""
    return {**json.loads(team.league_info), "espn_s2": espn_s2, "swid": swid}


def _legacy_row(user, provider, account, secrets, updated_at):
    """A row as 0005-era code wrote it: the key exactly as pasted, updated_at chosen."""
    ciphertext, version = crypto.encrypt(json.dumps(secrets))
    (row_id,) = db.execute_sql(
        "INSERT INTO usr.provider_connections"
        " (user_id, provider, external_account_id, secret_ciphertext, key_version, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (user.user_id, provider, account, ciphertext, version, updated_at),
    ).fetchone()
    return row_id


def _columns():
    return {r[0] for r in db.execute_sql(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'usr' AND table_name = 'provider_connections'"
    ).fetchall()}


def test_0024_merges_spellings_of_one_account_into_the_newest(user):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    old = _legacy_row(user, "espn", SWID.lower(), {"espn_s2": "AEB-old", "swid": SWID.lower()}, t0)
    new = _legacy_row(user, "espn", SWID.strip("{}"), {"espn_s2": "AEB-new", "swid": SWID.strip("{}")},
                      t0 + timedelta(days=3))
    other = _legacy_row(user, "espn", OTHER, {"espn_s2": "AEB-other", "swid": OTHER}, t0)
    yahoo = _legacy_row(user, "yahoo", "", {"yahoo_refresh_token": "rt"}, t0)
    on_old, on_new, on_other = _team(user, "A", old), _team(user, "B", new), _team(user, "C", other)

    db.execute_sql(FORWARD.read_text())

    rows = {c.id: c for c in ProviderConnection.select().where(ProviderConnection.user == user.user_id)}
    assert set(rows) == {new, other, yahoo}, "the older spelling is merged away"
    assert rows[new].external_account_id == SWID and rows[other].external_account_id == OTHER
    assert rows[yahoo].external_account_id == ""
    assert {t.team_id: t.provider_connection_id for t in Team.select()} == {
        on_old.team_id: new, on_new.team_id: new, on_other.team_id: other,
    }, "no team is left unlinked by the merge"
    assert credential_service.load_provider_tokens(user.user_id, new)["espn_s2"] == "AEB-new"

    db.execute_sql(FORWARD.read_text())                                  # idempotent
    assert ProviderConnection.select().where(ProviderConnection.user == user.user_id).count() == 3


def test_0024_rollback_applies_and_the_forward_file_restores_it(integration_db):
    try:
        db.execute_sql(ROLLBACK.read_text())
        assert not {"verified_at", "auth_failed_at"} & _columns()
    finally:
        db.execute_sql(FORWARD.read_text())                              # the chain's state is untouched
    assert {"verified_at", "auth_failed_at"} <= _columns()


def test_one_row_per_account_however_the_swid_is_spelled(user):
    first, created = credential_service.store_espn_cookies(user.user_id, "AEB-1", SWID.lower())
    again, created_again = credential_service.store_espn_cookies(user.user_id, "AEB-2", SWID.strip("{}"))
    assert created and not created_again and first == again
    row = ProviderConnection.get_by_id(first)
    assert row.external_account_id == SWID and row.verified_at is not None
    assert credential_service.load_provider_tokens(user.user_id, first) == {"espn_s2": "AEB-2", "swid": SWID}


def test_a_refresh_through_one_team_reaches_every_team_on_the_account(user):
    """The rotation case: a new espn_s2 pasted into one team's edit form."""
    a, b = _team(user, "A"), _team(user, "B")
    for team in (a, b):
        credential_service.persist(user.user_id, team, _payload(team, "AEB-old", SWID))
    credential_service.persist(user.user_id, a, _payload(a, "AEB-new", SWID.lower()))

    b = Team.get_by_id(b.team_id)
    assert b.provider_connection_id == a.provider_connection_id
    assert credential_service.hydrate(b, {})["espn_s2"] == "AEB-new"


def test_resaving_the_same_cookies_changes_nothing(user):
    """Every team edit re-persists the cookies it merged from the store."""
    team = _team(user, "A")
    credential_service.persist(user.user_id, team, _payload(team, "AEB", SWID))
    credential_service.mark_checked(team.provider_connection_id, ok=True)
    before = ProviderConnection.get_by_id(team.provider_connection_id)

    credential_service.persist(user.user_id, team, _payload(team, "AEB", SWID))

    after = ProviderConnection.get_by_id(team.provider_connection_id)
    assert (after.updated_at, after.verified_at) == (before.updated_at, before.verified_at)


def test_new_cookies_clear_the_old_verdict(user):
    connection_id, _ = credential_service.store_espn_cookies(user.user_id, "AEB-1", SWID)
    credential_service.mark_checked(connection_id, ok=False)
    team = _team(user, "A")

    credential_service.persist(user.user_id, team, _payload(team, "AEB-2", SWID))

    row = ProviderConnection.get_by_id(connection_id)
    assert row.auth_failed_at is None and row.verified_at is None


def test_the_view_lists_teams_and_never_the_swid(user):
    connection_id, _ = credential_service.store_espn_cookies(user.user_id, "AEB", SWID)
    team = _team(user, "Goblins", connection_id)

    (view,) = connection_service._views(user.user_id)

    assert view.id == connection_id and view.status == "ok" and view.account_hint == "…E5F6"
    assert [(t.team_id, t.team_name) for t in view.teams] == [(team.team_id, "Goblins")]
    assert SWID.strip("{}") not in view.model_dump_json()


def test_delete_unlinks_the_teams_and_is_owner_scoped(user):
    connection_id, _ = credential_service.store_espn_cookies(user.user_id, "AEB", SWID)
    team = _team(user, "A", connection_id)
    stranger = User.create(email="other@courtvision.dev", clerk_user_id="user_other",
                           created_at=datetime.now(timezone.utc))

    assert credential_service.delete_connection(stranger.user_id, connection_id) is None
    assert credential_service.delete_connection(user.user_id, connection_id) == [team.team_id]
    assert Team.get_by_id(team.team_id).provider_connection_id is None


def test_a_new_team_finds_the_users_only_connection(user):
    credential_service.store_espn_cookies(user.user_id, "AEB", SWID)
    resolved = TeamService._resolve_connection_handle(
        user.user_id, LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027))
    assert resolved.espn_s2 == "AEB" and resolved.swid == SWID


def test_tracked_teams_are_the_users_espn_teams_only(user):
    espn_team = _team(user, "A")
    Team.create(user_id=user.user_id, team_identifier="9Y", league_info=json.dumps(
        {"provider": "yahoo", "league_id": 9, "team_name": "Y", "year": 2027}))

    assert connection_service._tracked_espn_teams(user.user_id) == [
        (espn_team.team_id, json.loads(espn_team.league_info))]


def test_an_accepted_connection_takes_over_the_users_teams_on_that_account(user):
    """Teams saved before the connection existed -- with their own copy of the
    cookies, or none -- join it; teams elsewhere stay put."""
    from schemas.connections import EspnAccountTeam

    def account_team(espn_team_id, name):
        return EspnAccountTeam(league_id=1234, season=2027, espn_team_id=espn_team_id, team_name=name)

    # Saved last season with its own plaintext copy; ESPN has since renamed it
    legacy = Team.create(user_id=user.user_id, team_identifier="1234A", league_info=json.dumps(
        {"provider": "espn", "league_id": 1234, "team_name": "A", "year": 2026, "espn_team_id": 2,
         "espn_s2": "AEB-OLD", "swid": SWID}))
    by_name = _team(user, "B")                                    # a public league, no cookies
    other_id, _ = credential_service.store_espn_cookies(user.user_id, "AEB-X", OTHER)
    elsewhere = _team(user, "Linked", other_id)                   # already on another account
    stranger = Team.create(user_id=user.user_id, team_identifier="9999B", league_info=json.dumps(
        {"provider": "espn", "league_id": 9999, "team_name": "B", "year": 2027}))
    connection_id, _ = credential_service.store_espn_cookies(user.user_id, "AEB-NEW", SWID)

    adopted = connection_service._adopt_account_teams(user.user_id, connection_id, [
        account_team(2, "A (renamed)"), account_team(5, "B"), account_team(7, "Linked"),
    ])

    assert adopted == [legacy.team_id, by_name.team_id]
    legacy = Team.get_by_id(legacy.team_id)
    assert legacy.provider_connection_id == connection_id
    assert not {"espn_s2", "swid"} & set(json.loads(legacy.league_info)), "the old copy is dropped"
    assert credential_service.hydrate(legacy, {})["espn_s2"] == "AEB-NEW"
    assert Team.get_by_id(elsewhere.team_id).provider_connection_id == other_id
    assert Team.get_by_id(stranger.team_id).provider_connection_id is None
    (view,) = connection_service._views(user.user_id, connection_id)
    assert [t.team_id for t in view.teams] == [legacy.team_id, by_name.team_id]

"""
Credentials must not reach the client.

The incident on 2026-08-27 had two halves. Encrypting them at rest was the
first; this is the second — the API used to return ESPN cookies and Yahoo
refresh tokens in every team response, because the edit form round-tripped them
and `update_team` did a full overwrite.
"""

import json
from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo, LeagueInfoPublic
from services import credential_service
from services.team_service import TeamService

ESPN = {"provider": "espn", "league_id": 5, "team_name": "T", "year": 2026,
        "espn_s2": "AEB-SECRET-COOKIE", "swid": "{guid-1}"}
YAHOO = {"provider": "yahoo", "league_id": 7, "team_name": "Y", "year": 2026,
         "yahoo_access_token": "at", "yahoo_refresh_token": "rt",
         "yahoo_token_expiry": "2026-01-01T00:00:00", "yahoo_team_key": "466.l.1.t.2"}


def _team(payload, connection_id=None, league_id=None):
    return SimpleNamespace(
        team_id=1, user_id=10, league_info=json.dumps(payload),
        provider_connection_id=connection_id, league_id=league_id, league=None,
    )


@pytest.mark.unit
class TestPublicModelCannotCarrySecrets:
    def test_no_credential_field_exists_on_the_model(self):
        """Structural, not behavioural: a field that does not exist cannot leak.

        This is what stops someone reintroducing the problem by adding a
        convenience field to the response model.
        """
        leaked = set(LeagueInfoPublic.model_fields) & credential_service.ALL_SECRET_FIELDS
        assert not leaked, f"LeagueInfoPublic can carry credentials: {leaked}"

    def test_internal_model_still_carries_them(self):
        """LeagueInfo is the working object that reaches the providers."""
        assert credential_service.ALL_SECRET_FIELDS <= set(LeagueInfo.model_fields)

    def test_serialising_a_populated_source_drops_the_values(self):
        public = LeagueInfoPublic.from_league_info(LeagueInfo(**ESPN))
        body = public.model_dump_json()
        assert "AEB-SECRET-COOKIE" not in body and "guid-1" not in body
        assert public.league_id == 5 and public.team_name == "T"

    def test_yahoo_team_key_survives(self):
        """It identifies a team; it is not a credential and the UI needs it."""
        public = LeagueInfoPublic.from_league_info(LeagueInfo(**YAHOO))
        assert public.yahoo_team_key == "466.l.1.t.2"
        assert "rt" not in public.model_dump_json()


@pytest.mark.unit
class TestTeamResponseNeverLeaks:
    def test_stored_espn_team(self, monkeypatch):
        monkeypatch.setattr(TeamService, "_to_team_response",
                            TeamService._to_team_response)  # no stub; exercise the real path
        resp = TeamService._to_team_response(_team(ESPN))
        body = resp.model_dump_json()
        assert "AEB-SECRET-COOKIE" not in body and "guid-1" not in body

    def test_migrated_team_reports_credentials_without_decrypting(self, monkeypatch):
        """A migrated team's league_info holds no secrets, but the UI must still
        know they exist — answered from the connection link, not by decrypting."""
        called = []
        monkeypatch.setattr(credential_service, "hydrate",
                            lambda *a, **k: called.append(1) or {})
        public_payload = {k: v for k, v in ESPN.items()
                          if k not in credential_service.ALL_SECRET_FIELDS}
        resp = TeamService._to_team_response(_team(public_payload, connection_id=7))
        assert resp.league_info.has_espn_credentials is True
        assert not called, "the response path decrypted credentials it does not need"

    def test_team_with_no_credentials_reports_false(self):
        public_payload = {k: v for k, v in ESPN.items()
                          if k not in credential_service.ALL_SECRET_FIELDS}
        resp = TeamService._to_team_response(_team(public_payload))
        assert resp.league_info.has_espn_credentials is False

    def test_yahoo_team_reports_the_yahoo_flag(self):
        resp = TeamService._to_team_response(_team(YAHOO, connection_id=3))
        assert resp.league_info.has_yahoo_credentials is True
        assert resp.league_info.has_espn_credentials is False


@pytest.mark.unit
class TestAbsentMeansKeep:
    """The change that makes the response split safe.

    `update_team` used to overwrite league_info wholesale, so a form that no
    longer knows the credentials would blank them on the next save.
    """

    @pytest.fixture
    def stored(self, monkeypatch):
        from db.models import teams as teams_module
        team = _team(ESPN)
        monkeypatch.setattr(teams_module.Team, "get_or_none",
                            classmethod(lambda cls, *a: team))
        monkeypatch.setattr(credential_service, "hydrate", lambda t, payload: {**payload})
        return team

    def test_blank_credentials_keep_the_stored_ones(self, stored):
        incoming = LeagueInfo(provider=FantasyProvider.ESPN, league_id=5,
                              team_name="Renamed", year=2026, espn_s2="", swid="")
        merged = TeamService._merge_stored_credentials(10, 1, incoming)
        assert merged.espn_s2 == "AEB-SECRET-COOKIE" and merged.swid == "{guid-1}"
        assert merged.team_name == "Renamed", "the edit itself must still apply"

    def test_supplied_credentials_win(self, stored):
        incoming = LeagueInfo(provider=FantasyProvider.ESPN, league_id=5,
                              team_name="T", year=2026, espn_s2="NEW", swid="{new}")
        merged = TeamService._merge_stored_credentials(10, 1, incoming)
        assert merged.espn_s2 == "NEW" and merged.swid == "{new}"

    def test_unknown_team_is_not_found(self, monkeypatch):
        from core.errors import NotFoundError
        from db.models import teams as teams_module
        monkeypatch.setattr(teams_module.Team, "get_or_none", classmethod(lambda cls, *a: None))
        with pytest.raises(NotFoundError):
            TeamService._merge_stored_credentials(10, 999, LeagueInfo(**ESPN))


@pytest.mark.unit
class TestOpaqueConnectionHandle:
    """Yahoo tokens used to travel as query parameters — in the OAuth redirect
    and on every `/yahoo/leagues` and `/yahoo/teams` call. They are now stored
    server-side and the browser holds only a connection id."""

    def test_handle_resolves_to_the_stored_tokens(self, monkeypatch):
        monkeypatch.setattr(credential_service, "load_provider_tokens",
                            lambda uid, cid: {"yahoo_access_token": "at",
                                              "yahoo_refresh_token": "rt"})
        incoming = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=7,
                              team_name="Y", year=2026, yahoo_connection_id=42)
        resolved = TeamService._resolve_connection_handle(10, incoming)
        assert resolved.yahoo_access_token == "at"
        assert resolved.yahoo_refresh_token == "rt"

    def test_another_users_handle_resolves_to_nothing(self, monkeypatch):
        """The id is a small integer, so ownership is the access control."""
        from core.errors import BadRequestError
        monkeypatch.setattr(credential_service, "load_provider_tokens",
                            lambda uid, cid: None)
        incoming = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=7,
                              team_name="Y", year=2026, yahoo_connection_id=99)
        with pytest.raises(BadRequestError):
            TeamService._resolve_connection_handle(10, incoming)

    def test_requests_without_a_handle_are_untouched(self):
        incoming = LeagueInfo(**ESPN)
        assert TeamService._resolve_connection_handle(10, incoming) is incoming

    def test_the_handle_is_never_persisted(self):
        """It is a transient pointer, not part of the team's stored state."""
        info = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=7, team_name="Y",
                          year=2026, yahoo_connection_id=42)
        assert "yahoo_connection_id" not in TeamService.serialize_league_info(info)

    def test_the_handle_is_not_echoed_back_to_the_client(self):
        assert "yahoo_connection_id" not in LeagueInfoPublic.model_fields

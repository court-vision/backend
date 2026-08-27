"""
Envelope encryption for stored provider credentials, and the dual-read that lets
the migration land before it is finished.
"""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from core import crypto
from services import credential_service


@pytest.fixture
def keys(monkeypatch):
    """Configure two key versions and reset the parsed-key cache around the test."""
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    from core.settings import settings

    monkeypatch.setattr(settings, "credential_keys", f"1:{k1},2:{k2}", raising=False)
    crypto.reset_cache()
    yield SimpleNamespace(v1=k1, v2=k2)
    crypto.reset_cache()


@pytest.fixture
def no_keys(monkeypatch):
    from core.settings import settings

    monkeypatch.setattr(settings, "credential_keys", "", raising=False)
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest.mark.unit
class TestCrypto:
    def test_round_trip_uses_the_newest_key(self, keys):
        ciphertext, version = crypto.encrypt("espn_s2-value")
        assert version == 2, "new ciphertext must use the highest configured version"
        assert crypto.decrypt(ciphertext, version) == "espn_s2-value"

    def test_ciphertext_does_not_contain_the_plaintext(self, keys):
        ciphertext, _ = crypto.encrypt("SWID={abc-123}")
        assert "SWID" not in ciphertext and "abc-123" not in ciphertext

    def test_old_versions_still_decrypt(self, keys):
        """A rotation must not orphan rows written under the previous key."""
        old = Fernet(keys.v1.encode()).encrypt(b"legacy").decode()
        assert crypto.decrypt(old, 1) == "legacy"

    def test_retired_key_version_fails_loudly(self, keys):
        with pytest.raises(crypto.CredentialDecryptionError, match="No key configured for version 9"):
            crypto.decrypt("whatever", 9)

    def test_tampered_ciphertext_is_rejected(self, keys):
        ciphertext, version = crypto.encrypt("value")
        tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")
        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt(tampered, version)

    def test_disabled_without_keys(self, no_keys):
        assert crypto.is_enabled() is False
        assert crypto.current_version() is None
        with pytest.raises(RuntimeError, match="not configured"):
            crypto.encrypt("x")

    def test_malformed_key_config_is_rejected(self, monkeypatch):
        from core.settings import settings

        monkeypatch.setattr(settings, "credential_keys", "1", raising=False)
        crypto.reset_cache()
        with pytest.raises(ValueError, match="1:<fernet-key>"):
            crypto.is_enabled()
        crypto.reset_cache()


ESPN_PAYLOAD = {
    "provider": "espn", "league_id": 5, "team_name": "T", "year": 2026,
    "espn_s2": "AEB-cookie", "swid": "{guid-1}", "scoring_preview": None,
}
YAHOO_PAYLOAD = {
    "provider": "yahoo", "league_id": 7, "team_name": "Y", "year": 2026,
    "yahoo_access_token": "at", "yahoo_refresh_token": "rt", "yahoo_token_expiry": "2026-01-01T00:00:00",
    "yahoo_team_key": "466.l.1.t.2",
}


@pytest.mark.unit
class TestSplitSecrets:
    def test_espn_secrets_are_separated(self):
        public, secrets = credential_service.split_secrets(ESPN_PAYLOAD)
        assert secrets == {"espn_s2": "AEB-cookie", "swid": "{guid-1}"}
        assert "espn_s2" not in public and "swid" not in public
        assert public["league_id"] == 5 and public["team_name"] == "T"

    def test_yahoo_secrets_are_separated_but_team_key_stays(self):
        public, secrets = credential_service.split_secrets(YAHOO_PAYLOAD)
        assert set(secrets) == {"yahoo_access_token", "yahoo_refresh_token", "yahoo_token_expiry"}
        # yahoo_team_key identifies the team, it is not a credential.
        assert public["yahoo_team_key"] == "466.l.1.t.2"

    def test_public_half_never_carries_another_provider_s_secrets(self):
        """A stray Yahoo token on an ESPN payload must still be stripped."""
        mixed = {**ESPN_PAYLOAD, "yahoo_refresh_token": "leaked"}
        public, _ = credential_service.split_secrets(mixed)
        assert "yahoo_refresh_token" not in public

    def test_espn_account_id_is_the_swid(self):
        assert credential_service._external_account_id("espn", {"swid": "{guid-1}"}) == "{guid-1}"
        assert credential_service._external_account_id("yahoo", {}) == ""


@pytest.mark.unit
class TestDualRead:
    def test_hydrate_is_a_passthrough_for_an_unmigrated_team(self, keys):
        team = SimpleNamespace(team_id=1, provider_connection_id=None)
        assert credential_service.hydrate(team, ESPN_PAYLOAD) == ESPN_PAYLOAD

    def test_hydrate_is_a_passthrough_when_disabled(self, no_keys):
        team = SimpleNamespace(team_id=1, provider_connection_id=42)
        assert credential_service.hydrate(team, ESPN_PAYLOAD) == ESPN_PAYLOAD

    def test_persist_is_a_noop_when_disabled(self, no_keys):
        team = SimpleNamespace(team_id=1, provider_connection_id=None, league_info="{}", save=lambda: None)
        assert credential_service.persist(1, team, ESPN_PAYLOAD) is None
        assert team.league_info == "{}", "league_info must be untouched when the store is off"

    def test_hydrate_merges_decrypted_secrets(self, keys, monkeypatch):
        ciphertext, version = crypto.encrypt(json.dumps({"espn_s2": "AEB-cookie", "swid": "{guid-1}"}))
        stored = SimpleNamespace(id=7, secret_ciphertext=ciphertext, key_version=version)

        class FakeConnection:
            @staticmethod
            def get_or_none(*_a, **_k):
                return stored
            id = SimpleNamespace()

        module = SimpleNamespace(ProviderConnection=FakeConnection)
        monkeypatch.setitem(__import__("sys").modules, "db.models.provider_connections", module)

        public, _ = credential_service.split_secrets(ESPN_PAYLOAD)
        team = SimpleNamespace(team_id=1, provider_connection_id=7)
        hydrated = credential_service.hydrate(team, public)

        assert hydrated["espn_s2"] == "AEB-cookie" and hydrated["swid"] == "{guid-1}"
        assert hydrated["league_id"] == 5, "non-secret fields survive the merge"

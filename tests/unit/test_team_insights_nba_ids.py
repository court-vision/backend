"""
Roster players are addressed by their provider's ID, but terminal panels
navigate by NBA player ID, so `TeamInsightsService._nba_ids` resolves the two.

The ESPN and Yahoo paths key their results differently on purpose: ESPN IDs map
straight to `nba.players.espn_id`, while Yahoo IDs share no namespace with it
and must go through names. The DB layer is stubbed.
"""

from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp
from services.team_insights_service import TeamInsightsService

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="T", year=2027)
YAHOO = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027)


def _player(player_id: int, name: str, team: str = "LAL") -> PlayerResp:
    return PlayerResp(
        player_id=player_id, name=name, avg_points=10.0,
        team=team, valid_positions=["PG"], injured=False,
    )


@pytest.fixture
def stub_players(monkeypatch):
    """Stand in for `PlayerModel.select(...).where(...)` with fixed rows."""
    def _field():
        # Peewee fields build an expression via `.in_()`; the stub only has to
        # be callable, since the stubbed `.where()` ignores what it is given.
        return SimpleNamespace(in_=lambda values: None)

    def _install(rows):
        monkeypatch.setattr(
            "services.nba_id_resolver.PlayerModel",
            SimpleNamespace(
                id=_field(), espn_id=_field(), name_normalized=_field(),
                select=lambda *a: SimpleNamespace(where=lambda *w: rows),
            ),
        )
    return _install


def test_espn_roster_keys_by_espn_id(stub_players):
    stub_players([
        SimpleNamespace(id=2544, espn_id=1966),
        SimpleNamespace(id=203999, espn_id=3112335),
    ])
    result = TeamInsightsService._nba_ids(
        ESPN, [_player(1966, "LeBron James"), _player(3112335, "Nikola Jokic")]
    )
    assert result == {1966: 2544, 3112335: 203999}


def test_yahoo_roster_keys_by_normalized_name(stub_players):
    stub_players([SimpleNamespace(id=2544, name_normalized="lebron james")])
    result = TeamInsightsService._nba_ids(YAHOO, [_player(7007, "LeBron James")])
    # Keyed by name, not by the Yahoo player_id it was given.
    assert result == {"lebron james": 2544}
    assert 7007 not in result


def test_yahoo_drops_ambiguous_names(stub_players):
    """Two NBA players sharing a normalized name resolve to neither.

    A null id costs the user navigation; a wrong id sends them to the wrong
    player's stat line, which is the worse failure.
    """
    stub_players([
        SimpleNamespace(id=201142, name_normalized="kevin durant"),
        SimpleNamespace(id=999999, name_normalized="kevin durant"),
        SimpleNamespace(id=2544, name_normalized="lebron james"),
    ])
    result = TeamInsightsService._nba_ids(
        YAHOO, [_player(1, "Kevin Durant"), _player(2, "LeBron James")]
    )
    assert "kevin durant" not in result
    assert result == {"lebron james": 2544}


def test_empty_roster_skips_the_query(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not query for an empty roster")
    monkeypatch.setattr(
        "services.nba_id_resolver.PlayerModel",
        SimpleNamespace(select=_boom),
    )
    assert TeamInsightsService._nba_ids(ESPN, []) == {}

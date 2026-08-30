"""
The Yahoo team-abbreviation map, from this repo's side of the boundary.

cv-core owns the map now; these tests pin what this repo's consumers
(yahoo_service, scoring/providers/yahoo_settings) depend on: Yahoo/ESPN team
forms normalize to the canonical nba.teams keys, PHI/PHX included — the pair
the two repos' hand-copied maps disagreed on for a season.
"""

import pytest

from utils.yahoo_helpers import YAHOO_TEAM_MAP, normalize_team_abbr

# The 30 abbreviations NBATeam.seed_teams() writes as nba.teams primary keys.
SEEDED = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}


@pytest.mark.unit
class TestCanonicalDirection:
    def test_philadelphia_and_phoenix_normalize_to_nba_keys(self):
        assert normalize_team_abbr("PHL") == "PHI"
        assert normalize_team_abbr("PHO") == "PHX"
        assert normalize_team_abbr("PHI") == "PHI"
        assert normalize_team_abbr("PHX") == "PHX"

    def test_every_map_value_is_a_seeded_team_key(self):
        stray = set(YAHOO_TEAM_MAP.values()) - SEEDED
        assert not stray, f"map emits abbreviations nba.teams does not seed: {stray}"

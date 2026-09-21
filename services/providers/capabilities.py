"""What a fantasy provider can do for a team, asked once instead of checked as
`provider == ESPN` at every feature.

Some answers are per provider (ESPN has a lineup write; Yahoo's is not built
yet), some per league (a Yahoo league with a weekly deadline has no daily
lineup), one per connection (the scope Yahoo granted). The adapter answers
`capabilities(league_info)` with all three folded in, and a feature reads the
record: `if not caps.lineup_write: ... reason="provider_not_supported"`. The
wire shape of a refusal is unchanged; only the question moved.

The Yahoo row is what is built today. docs/YAHOO_PARITY_PLAN.md flips its
fields phase by phase (P5 the board, P6 the writes, P9 draft import).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from core.errors import BadRequestError
from schemas.common import FantasyProvider

PROVIDER_NOT_SUPPORTED = "PROVIDER_NOT_SUPPORTED"


@dataclass(frozen=True)
class ProviderCapabilities:
    lineup_read: bool          # a per-day board with slots, eligibility and locks
    lineup_write: bool         # slot moves through the writer
    transactions: bool         # add / drop / add+drop through the writer
    waiver_claims: bool        # ESPN: refused today; Yahoo: not built
    position_limits: bool      # ESPN's per-position roster caps; Yahoo has slot counts only
    live_totals: bool          # the provider's own matchup totals move during games
    daily_lineups: bool        # False for a Yahoo league with a weekly deadline
    account_teams: bool        # can list every team on the account
    draft_sync: bool           # a live draft feed (ESPN, through the extension)
    draft_import: bool         # completed draft results
    write_scope: bool = True   # this connection's grant includes write (Yahoo: fspt-w)

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


ESPN_CAPABILITIES = ProviderCapabilities(
    lineup_read=True,
    lineup_write=True,
    transactions=True,
    waiver_claims=False,
    position_limits=True,
    live_totals=False,
    daily_lineups=True,
    account_teams=True,
    draft_sync=True,
    draft_import=True,
)

YAHOO_CAPABILITIES = ProviderCapabilities(
    lineup_read=False,
    lineup_write=False,
    transactions=False,
    waiver_claims=False,
    position_limits=False,
    live_totals=False,
    daily_lineups=True,
    account_teams=False,
    draft_sync=False,
    draft_import=False,
    write_scope=False,
)

_LABELS = {"espn": "ESPN", "yahoo": "Yahoo"}

# The feature as the user knows it, for the empty-state message a refused
# capability renders. Keyed by feature, not capability: daily actions and the
# lineup editor both need `lineup_read` but say different things.
FEATURES = {
    "lineup_editing": "Lineup editing",
    "lineup_changes": "Lineup changes",
    "daily_actions": "Daily actions",
    "roster_changes": "Roster changes",
    "lineup_checks": "Lineup checks",
    "draft_import": "Draft import",
}


def provider_name(provider: FantasyProvider | str) -> str:
    value = provider.value if hasattr(provider, "value") else str(provider)
    return _LABELS.get(value, value.upper())


def unavailable_message(feature: str, provider: FantasyProvider | str) -> str:
    """"Lineup editing is not available for Yahoo teams yet"."""
    return f"{FEATURES.get(feature, feature)} is not available for {provider_name(provider)} teams yet"


class ProviderCapabilityMissing(BadRequestError):
    """A feature asked a provider for something its adapter does not do yet.

    400 PROVIDER_NOT_SUPPORTED, the code the frontend already maps. Raised by
    an adapter method that is not built for the provider; features that can
    answer with an empty state check `capabilities()` first and never see it.
    """

    def __init__(self, provider: FantasyProvider | str, feature: str, message: Optional[str] = None):
        super().__init__(PROVIDER_NOT_SUPPORTED, message or unavailable_message(feature, provider))
        self.provider = provider.value if hasattr(provider, "value") else str(provider)
        self.feature = feature

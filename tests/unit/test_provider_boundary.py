"""The provider boundary: both adapters satisfy the whole Protocol, a feature
asks `capabilities()` instead of the provider's name, an operation a provider
lacks refuses with the code the client already maps, and the one lineup
vocabulary round-trips Yahoo's slots and statuses."""

import asyncio
import inspect

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from services.lineup_planner import IR_STATUSES, OUT_STATUSES
from services.providers import (
    FantasyProviderAdapter,
    ProviderCapabilityMissing,
    RosterWriter,
    get_provider_adapter,
    get_roster_writer,
    unavailable_message,
)
from services.providers import vocab
from services.providers.adapters import EspnAdapter, YahooAdapter
from services.providers.capabilities import ESPN_CAPABILITIES, YAHOO_CAPABILITIES, ProviderCapabilities

YAHOO = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="Y", year=2027, yahoo_team_key="466.l.1.t.3")
ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="E", year=2027)

PROTOCOL_METHODS = [
    name for name, member in inspect.getmembers(FantasyProviderAdapter)
    if not name.startswith("_") and callable(member)
]


@pytest.mark.unit
@pytest.mark.parametrize("adapter", [EspnAdapter(), YahooAdapter()], ids=["espn", "yahoo"])
def test_every_adapter_implements_the_whole_protocol(adapter):
    """A method added to the Protocol without both implementations is caught here,
    not by the first request that needs it."""
    assert PROTOCOL_METHODS, "the Protocol lost its methods"
    missing = [name for name in PROTOCOL_METHODS if not callable(getattr(adapter, name, None))]
    assert missing == []
    assert isinstance(adapter, FantasyProviderAdapter)
    assert adapter.identity_kind in ("espn_id", "name")
    assert adapter.uses_name_identity == (adapter.identity_kind == "name")


@pytest.mark.unit
def test_the_registry_answers_enum_and_string():
    assert get_provider_adapter("yahoo").provider is FantasyProvider.YAHOO
    assert get_provider_adapter(FantasyProvider.ESPN).provider is FantasyProvider.ESPN
    for provider in FantasyProvider:
        assert isinstance(get_roster_writer(provider), RosterWriter)


@pytest.mark.unit
def test_capabilities_are_a_complete_record_per_provider():
    for caps in (ESPN_CAPABILITIES, YAHOO_CAPABILITIES):
        assert isinstance(caps, ProviderCapabilities)
        assert set(caps.as_dict()) == set(ProviderCapabilities.__dataclass_fields__)
    assert get_provider_adapter("espn").capabilities(ESPN) == ESPN_CAPABILITIES
    assert get_provider_adapter("yahoo").capabilities(YAHOO) == YAHOO_CAPABILITIES
    # What is built today: ESPN writes, Yahoo reads (docs/YAHOO_PARITY_PLAN.md flips these)
    assert ESPN_CAPABILITIES.lineup_write and ESPN_CAPABILITIES.transactions
    assert not YAHOO_CAPABILITIES.lineup_read and not YAHOO_CAPABILITIES.write_scope


@pytest.mark.unit
def test_an_operation_yahoo_lacks_refuses_with_the_mapped_code():
    adapter = get_provider_adapter("yahoo")
    with pytest.raises(ProviderCapabilityMissing) as excinfo:
        asyncio.run(adapter.read_lineup(1, YAHOO))
    assert excinfo.value.error_code == "PROVIDER_NOT_SUPPORTED"
    assert excinfo.value.message == unavailable_message("lineup_editing", "yahoo")
    with pytest.raises(ProviderCapabilityMissing):
        asyncio.run(adapter.player_pool_entries(YAHOO, [1]))
    with pytest.raises(ProviderCapabilityMissing):
        asyncio.run(get_roster_writer("yahoo").apply_lineup(YAHOO, None, [], "k"))


@pytest.mark.unit
def test_messages_name_the_feature_and_the_provider():
    assert unavailable_message("lineup_editing", "yahoo") == "Lineup editing is not available for Yahoo teams yet"
    assert unavailable_message("daily_actions", FantasyProvider.ESPN) == "Daily actions is not available for ESPN teams yet".replace("actions is", "actions is")


@pytest.mark.unit
def test_yahoo_slots_round_trip_through_the_canonical_ids():
    for name, slot_id in vocab.YAHOO_SLOT_IDS.items():
        assert vocab.yahoo_slot_id(name) == slot_id
        back = vocab.yahoo_slot_name(slot_id, current=name)
        assert back == name or (name == "IL+" and back == "IL+") or (name != "IL+" and back == vocab.YAHOO_SLOT_NAMES[slot_id])
    assert vocab.yahoo_slot_name(13) == "IL" and vocab.yahoo_slot_name(13, current="IL+") == "IL+"
    assert vocab.yahoo_slot_id("Util") == 11 and vocab.slot_name(11) == "UT"
    assert vocab.yahoo_slot_id("BN") == vocab.BENCH_SLOT_ID and vocab.yahoo_slot_id("IL") == vocab.IR_SLOT_ID
    assert vocab.yahoo_slot_id("PG/SG") is None
    assert vocab.NA_SLOT_ID not in vocab.ACTIVE_SLOT_IDS


@pytest.mark.unit
def test_yahoo_statuses_land_in_the_planners_vocabulary():
    for status in ("O", "INJ", "IL", "IL+", "SUSP", "NA"):
        assert vocab.normalize_injury_status("yahoo", status) in OUT_STATUSES
    assert vocab.normalize_injury_status("yahoo", "SUSP") not in IR_STATUSES
    assert vocab.normalize_injury_status("yahoo", "GTD") == "GTD"
    assert vocab.normalize_injury_status("yahoo", "D") == "DTD"
    assert vocab.normalize_injury_status("yahoo", "") is None
    assert vocab.normalize_injury_status("espn", "ACTIVE") is None
    assert vocab.normalize_injury_status("espn", "day_to_day") == "DAY_TO_DAY"

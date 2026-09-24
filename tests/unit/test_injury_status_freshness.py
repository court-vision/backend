"""An injury report is a current status only while it is recent.

data-platform writes injury status once per NBA date with games, so a report
stops being refreshed when the games stop. On 2026-09-21 the status endpoint
served Alperen Sengun as "Out" off a 2026-04-12 report: April's report read
as September's status. The SQL side of the window is covered by
tests/integration/test_player_injury_freshness.py.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from db.models.nba import player_injuries
from db.models.nba.player_injuries import (
    CURRENT_REPORT_MAX_AGE_DAYS,
    PlayerInjury,
    current_report_window,
)
from services import player_service
from services.player_service import PlayerService

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 21)
get_player_status = PlayerService.get_player_status.__wrapped__  # skip the DB executor


def test_window_is_the_last_seven_days_inclusive():
    assert CURRENT_REPORT_MAX_AGE_DAYS == 7
    assert current_report_window(TODAY) == (date(2026, 9, 14), TODAY)


def test_window_spans_the_all_star_break():
    # 2025-26: last games Feb 12, resumed Feb 19, so no report was written in
    # between. The Feb 12 report is still the current one on the morning of
    # Feb 19, before that day's pre-game run.
    oldest, _ = current_report_window(date(2026, 2, 19))
    assert oldest <= date(2026, 2, 12)
    oldest, _ = current_report_window(date(2026, 2, 20))
    assert oldest > date(2026, 2, 12)


def test_window_excludes_last_seasons_final_report():
    oldest, _ = current_report_window(TODAY)
    assert date(2026, 4, 12) < oldest


def test_window_defaults_to_todays_nba_date(monkeypatch):
    monkeypatch.setattr(player_injuries, "nba_date_et", lambda: TODAY)
    assert current_report_window() == (date(2026, 9, 14), TODAY)


@pytest.fixture
def current_status(monkeypatch):
    """Stub the model read; `state["report"]` is what it finds, `state["as_of"]` what it was asked."""
    state = {"report": None, "as_of": None}

    def fake_get_current_status(player_id, as_of=None):
        state["as_of"] = as_of
        return state["report"]

    monkeypatch.setattr(player_service, "nba_date_et", lambda: TODAY)
    monkeypatch.setattr(PlayerInjury, "get_current_status", staticmethod(fake_get_current_status))
    return state


def test_status_is_read_on_todays_nba_date(current_status):
    get_player_status(1630578)
    assert current_status["as_of"] == TODAY


def test_no_current_report_is_no_status(current_status):
    resp = get_player_status(1630578)
    assert resp.data is None
    assert resp.message == "No current injury report"


def test_current_report_carries_its_age(current_status):
    current_status["report"] = SimpleNamespace(
        status="Questionable",
        injury_type=None,
        injury_detail=None,
        expected_return=None,
        report_date=date(2026, 9, 19),
    )
    data = get_player_status(1630578).data
    assert data.status == "Questionable"
    assert data.report_date == "2026-09-19"
    assert data.report_age_days == 2

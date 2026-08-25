"""GET /v1/schedule/weeks carries every week plus the season block."""

import pytest

from core.settings import settings
from services import schedule_service as ss


@pytest.fixture
def season_25_26(monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2025-26")
    ss.reset_cache()
    yield
    ss.reset_cache()


@pytest.mark.api
def test_weeks_and_season_block(client, season_25_26):
    res = client.get("/v1/schedule/weeks")
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data["weeks"]) == 24
    assert data["weeks"][0] == {"week": 1, "start_date": "2025-10-21", "end_date": "2025-10-26", "game_span": 6}
    assert data["weeks"][16]["game_span"] == 14
    s = data["season"]
    assert s["key"] == "2025-26" and s["label"] == "2025–26" and s["espn_year"] == 2026
    assert s["regular_season_start"] == "2025-10-21" and s["regular_season_end"] == "2026-04-12"
    assert s["preseason_start"] == "2025-10-02" and s["week_count"] == 24
    assert s["phase"] in ("preseason", "regular", "offseason")
    assert data["current_week"] is None or 1 <= data["current_week"] <= 24

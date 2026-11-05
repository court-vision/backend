"""
HTTP contracts for step 6 of the Dime autopilot plan: POST /v1/internal/jobs/checkins/events
(data-platform's posts of verdict changes and news items, behind the pipeline token) and the
public news reads, GET /v1/news (no blurbs) and GET /v1/players/{id}/news.
"""

from datetime import date, datetime, timezone

import pytest

from core import pipeline_auth
from services import news_service
from services.ai import checkins, events

pytestmark = pytest.mark.api

EVENTS = "/v1/internal/jobs/checkins/events"
AUTH = {"Authorization": "Bearer pipe-token"}
AT = datetime(2026, 11, 5, 3, 12, tzinfo=timezone.utc)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(pipeline_auth, "PIPELINE_API_TOKEN", "pipe-token")


def item(news_id=1):
    return news_service.Item(id=news_id, source="rotowire_rss", kind="injury", headline="Vit Krejci: Suffers leg fracture",
                             body="Krejci suffered a left leg fracture.", url="https://www.rotowire.com/x",
                             attribution="Rotowire", published_at=AT, players=((1630249, "Vit Krejci"),), teams=("ATL",))


def test_events_need_the_pipeline_token(client, token):
    assert client.post(EVENTS, json={}).status_code in (401, 403)
    assert client.post(EVENTS, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_events_reach_the_receiver_as_keys_and_answer_what_started(client, token, monkeypatch):
    calls = []

    async def receive(availability, news):
        calls.append((availability, news))
        return {"off": False, "teams": 2, "facts": 2, "recorded": 1,
                "started": [checkins.Started(41, 23, checkins.EVENT, date(2026, 11, 10))]}

    monkeypatch.setattr(events, "receive", receive)
    r = client.post(EVENTS, json={"availability": [{"player_id": 1628973, "game_date": "2026-11-10"}],
                                  "news": [7]}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert calls == [([(1628973, date(2026, 11, 10))], [7])]
    body = r.json()
    assert body["message"] == "Recorded 1 event(s)"
    assert body["data"] == {"off": False, "teams": 2, "facts": 2, "recorded": 1,
                            "started": [{"checkin_id": 41, "team_id": 23, "kind": "event", "date": "2026-11-10"}]}


def test_events_switched_off_say_so(client, token, monkeypatch):
    async def receive(availability, news):
        return {"off": True, "teams": 0, "facts": 0, "recorded": 0, "started": []}

    monkeypatch.setattr(events, "receive", receive)
    r = client.post(EVENTS, json={"news": [7]}, headers=AUTH)
    assert r.status_code == 200 and r.json()["message"] == "Events are off"


@pytest.mark.parametrize("body", [
    {"availability": [{"player_id": 0, "game_date": "2026-11-10"}]},
    {"availability": [{"player_id": 1, "game_date": "tonight"}]},
    {"news": list(range(501))},
])
def test_a_malformed_post_is_422(client, token, body):
    assert client.post(EVENTS, json=body, headers=AUTH).status_code == 422


def test_the_league_feed_has_no_blurbs(client, monkeypatch):
    calls = []

    async def news_feed(**kwargs):
        calls.append(kwargs)
        return [item()]

    monkeypatch.setattr(news_service, "news_feed", news_feed)
    r = client.get("/v1/news?kind=injury&kind=availability&team=ATL&limit=5")
    assert r.status_code == 200, r.text
    assert calls[0]["kinds"] == ["injury", "availability"] and calls[0]["team"] == "ATL" and calls[0]["limit"] == 5
    first = r.json()["data"]["items"][0]
    assert first["headline"] == "Vit Krejci: Suffers leg fracture" and first["body"] is None
    assert first["players"] == [{"player_id": 1630249, "name": "Vit Krejci"}] and first["teams"] == ["ATL"]


@pytest.mark.parametrize("query", ["kind=gossip", "team=Hawks", "limit=51"])
def test_the_league_feed_refuses_what_it_can_t_serve(client, query):
    assert client.get(f"/v1/news?{query}").status_code == 422


def test_a_player_s_news_carries_the_blurb(client, monkeypatch):
    calls = []

    async def player_news(player_id, *, days=7, limit=20, now=None):
        calls.append((player_id, days, limit))
        return [item()]

    monkeypatch.setattr(news_service, "player_news", player_news)
    r = client.get("/v1/players/1630249/news?days=3")
    assert r.status_code == 200, r.text
    assert calls == [(1630249, 3, 20)]
    assert r.json()["data"]["items"][0]["body"] == "Krejci suffered a left leg fracture."

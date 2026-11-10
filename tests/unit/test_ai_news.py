"""
Dime's news tools (the Dime autopilot plan, step 6; services/ai/news.py) and the news
get_player_status now carries: what each returns, the bounds on what the model may ask
for, and a roster ESPN won't serve. news_service is stubbed; its queries run against the
real schema in tests/integration/test_dime_events.py.
"""

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from schemas.common import ApiStatus
from schemas.player import PlayerStatusResp
from services import news_service
from services.ai import news as ai_news
from services.ai import tools
from services.player_service import PlayerService

pytestmark = [pytest.mark.unit]

AT = datetime(2026, 11, 5, 3, 12, tzinfo=timezone.utc)


def _run(name, raw, ctx=None):
    return asyncio.run(tools.run_tool(name, raw, ctx=ctx))


def item(news_id=1, body="b" * 500, kind="injury"):
    return news_service.Item(id=news_id, source="rotowire_rss", kind=kind, headline="Vit Krejci: Suffers leg fracture",
                             body=body, url="https://www.rotowire.com/x", attribution="Rotowire", published_at=AT,
                             players=((1630249, "Vit Krejci"),), teams=("ATL",))


class TestItemView:
    def test_an_item_reads_with_its_source_time_players_and_teams(self):
        view = item(body="short").view()
        assert view == {
            "news_id": 1, "published_at": "2026-11-05T03:12:00+00:00", "source": "rotowire_rss", "kind": "injury",
            "headline": "Vit Krejci: Suffers leg fracture", "attribution": "Rotowire", "url": "https://www.rotowire.com/x",
            "players": [{"player_id": 1630249, "name": "Vit Krejci"}], "teams": ["ATL"], "body": "short",
        }

    def test_the_blurb_is_cut_for_dime_and_left_out_of_the_league_feed(self):
        assert item().view(body_chars=300)["body"] == "b" * 300 + "…"
        assert "body" not in item().view(body=False)


@pytest.fixture
def stubs(monkeypatch):
    state = SimpleNamespace(player_calls=[], feed_calls=[], roster=frozenset({1630249}), roster_calls=[])

    async def player_news(player_id, *, days=7, limit=20, now=None):
        state.player_calls.append((player_id, days, limit))
        return [item()]

    async def news_feed(**kwargs):
        state.feed_calls.append(kwargs)
        return [item(news_id=2)]

    async def owned_team(ctx, team_id):
        return SimpleNamespace(team_id=team_id, user_id=ctx.user_id)

    async def roster_ids(team_id, user_id):
        state.roster_calls.append((team_id, user_id))
        return state.roster

    from services.ai import events
    monkeypatch.setattr(news_service, "player_news", player_news)
    monkeypatch.setattr(news_service, "news_feed", news_feed)
    monkeypatch.setattr(tools, "_owned_team", owned_team)
    monkeypatch.setattr(events, "roster_ids", roster_ids)
    return state


CTX = SimpleNamespace(user_id=42)


class TestGetPlayerNews:
    def test_it_returns_the_player_s_items_with_the_blurb_cut_short(self, stubs):
        out = _run("get_player_news", {"player_id": 1630249, "days": 3})
        assert not out.is_error
        body = json.loads(out.content)
        assert (body["player_id"], body["days"]) == (1630249, 3)
        assert stubs.player_calls == [(1630249, 3, 8)]
        assert body["items"][0]["body"] == "b" * news_service.BODY_CHARS + "…"
        assert body["items"][0]["source"] == "rotowire_rss"

    @pytest.mark.parametrize("raw", [{"player_id": 0}, {"player_id": 1, "days": 31}, {"player_id": 1, "limit": 21},
                                     {"player_id": 1, "since": "yesterday"}])
    def test_out_of_bounds_asks_are_errors(self, stubs, raw):
        assert _run("get_player_news", raw).is_error
        assert stubs.player_calls == []


class TestGetNews:
    def test_the_league_feed_has_no_blurbs(self, stubs):
        body = json.loads(_run("get_news", {"kinds": ["injury", "availability"], "hours": 6}).content)
        assert stubs.feed_calls[0]["kinds"] == ["injury", "availability"]
        assert stubs.feed_calls[0]["hours"] == 6 and stubs.feed_calls[0]["player_ids"] is None
        assert "body" not in body["items"][0] and "team_id" not in body

    def test_a_team_s_feed_is_its_roster_s_news(self, stubs):
        body = json.loads(_run("get_news", {"team_id": 23}, ctx=CTX).content)
        assert stubs.roster_calls == [(23, 42)]
        assert stubs.feed_calls[0]["player_ids"] == frozenset({1630249})
        assert body["team_id"] == 23 and len(body["items"]) == 1

    def test_a_roster_espn_won_t_serve_says_so_rather_than_answering_for_the_league(self, stubs):
        stubs.roster = None
        body = json.loads(_run("get_news", {"team_id": 23}, ctx=CTX).content)
        assert body["items"] == [] and "couldn't be read" in body["note"]
        assert stubs.feed_calls == []

    def test_an_nba_team_s_feed_goes_by_tricode(self, stubs):
        _run("get_news", {"nba_team": "nyk"})
        assert stubs.feed_calls[0]["team"] == "nyk"

    @pytest.mark.parametrize("raw", [{"nba_team": "Knicks"}, {"kinds": ["gossip"]}, {"hours": 0}, {"hours": 169},
                                     {"limit": 31}])
    def test_out_of_bounds_asks_are_errors(self, stubs, raw):
        assert _run("get_news", raw).is_error


class TestDefinitions:
    def test_the_news_tools_are_appended_last_and_every_toolset_has_them(self):
        names = [t["name"] for t in tools.AGENT_TOOLS]
        # Appended after the decisions; only the draft recap (step 7) came after them
        assert names[-3:-1] == ["get_player_news", "get_news"] and names[-1] == "get_draft_recap"
        from services.ai import agent
        assert {"get_player_news", "get_news"} <= agent.ALLOWED
        assert {"get_player_news", "get_news"} <= agent.CHECKIN_ALLOWED

    def test_the_definitions_match_the_inputs(self):
        for tool, model in ((ai_news.GET_PLAYER_NEWS, ai_news.PlayerNewsInput), (ai_news.GET_NEWS, ai_news.NewsInput)):
            assert set(tool["input_schema"]["properties"]) == set(model.model_fields)
            assert tool["input_schema"]["additionalProperties"] is False
        kinds = ai_news.GET_NEWS["input_schema"]["properties"]["kinds"]["items"]["enum"]
        assert tuple(kinds) == news_service.NEWS_KINDS


class TestPlayerStatus:
    def test_it_carries_the_verdict_and_the_newest_item(self, monkeypatch):
        async def status(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="none", data=None)

        async def verdict_for(player_id, today):
            return {"game_date": "2026-11-05", "verdict": "out", "decided_by": "espn_injuries", "final": False}

        async def latest_item(player_id, **kwargs):
            return item()

        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(status))
        monkeypatch.setattr(news_service, "verdict_for", verdict_for)
        monkeypatch.setattr(news_service, "latest_item", latest_item)
        body = json.loads(_run("get_player_status", {"player_id": 1630249}).content)
        assert body["availability"]["verdict"] == "out"
        assert body["latest_news"]["headline"].startswith("Vit Krejci")
        assert len(body["latest_news"]["body"]) == news_service.BODY_CHARS + 1

    def test_a_failed_news_read_leaves_the_report_alone(self, monkeypatch):
        async def status(player_id):
            return PlayerStatusResp(status=ApiStatus.SUCCESS, message="none", data=None)

        async def broken(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(PlayerService, "get_player_status", staticmethod(status))
        monkeypatch.setattr(news_service, "verdict_for", broken)
        out = _run("get_player_status", {"player_id": 1630249})
        assert not out.is_error and json.loads(out.content) == {"player_id": 1630249, "injury": None}

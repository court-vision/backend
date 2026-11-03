"""
The app map's search (services/ai/app_map.py): the screens a question finds,
weighed by their trails and the app's own words, never by the crawled data;
the copy no screen showed, apart; and what `find` hands the agent.
"""

from datetime import datetime, timezone

import pytest

from schemas.app_map import AppMapUpload
from services.ai.app_map import TREE_CHARS, LoadedMap, tokens

pytestmark = pytest.mark.unit

BUILD = "21a5238c0ffee21a5238c0ffee21a5238c0ffee2"


def screen(id, desk, trail, tree, link=None, signed_in=False):
    return {"id": id, "desk": desk, "title": " › ".join(s["name"] for s in trail) or desk.title(), "trail": trail,
            "url": f"/{desk}", "link": link, "linkVaries": [], "tree": tree, "fingerprint": id, "signedIn": signed_in}


def step(name, role="button", **kw):
    return {"role": role, "name": name, **kw}


MAP = {
    "version": 1, "build": BUILD, "crawledAt": "2026-10-20T07:00:00Z",
    "desks": [
        {"id": "week", "label": "Week", "href": "/week", "note": "Your team's matchup week",
         "keywords": ["matchup", "lineup", "streamers", "market"], "needs": "account", "keys": "V view · P market"},
        {"id": "scout", "label": "Scout", "href": "/scout", "note": "Players, teams and nights",
         "keywords": ["player", "team"], "needs": None, "keys": None},
        {"id": "account", "label": "Account", "href": "/account", "note": "Teams, connections and alerts",
         "keywords": ["settings"], "needs": "account", "keys": None},
    ],
    "screens": [
        screen("week", "week", [], "- heading \"Week\"\n- button \"Market P\"", link="/week"),
        screen("week/market-p", "week", [step("Market P")],
               "- radiogroup \"Market\":\n  - radio \"ONE DAY\"\n  - radio \"REST OF WEEK\"\n- text: Free agents to pick up",
               link="/week?market=1", signed_in=True),
        screen("week/players", "week", [step("PLAYERS", role="radio")], "- row \"Tyrese Maxey PHI G\"", link="/week?view=players"),
        # Data in a tree: Jokić is not the app's word for anything
        screen("scout", "scout", [], "- listbox \"Pool\":\n  - option \"1 N. Jokić DEN C\"\n- button \"Pin P\"", link="/scout"),
        screen("scout/pool-option", "scout", [step("1 N. Jokić DEN C", role="option", example=True, within="Pool")],
               "- heading \"Nikola Jokić\"\n- text: Game log\n- text: Per game"),
        screen("account/connect-espn", "account", [step("Connect ESPN…")],
               "- dialog \"Connect ESPN\":\n  - text: Cookies from a signed-in ESPN tab\n  - button \"Connect\""),
        screen("account/alerts", "account", [step("Lineup alerts", role="link")], "- switch \"Email me before tip-off\"",
               link="/account?v=alerts"),
    ],
    "copy": [
        {"file": "src/components/week/Market.tsx", "desk": "week", "seen": ["Market", "ONE DAY", "Free agents to pick up"],
         "unseen": ["No free agents fit this spot"]},
        {"file": "src/components/scout/PlayerSheet.tsx", "desk": "scout", "seen": ["Game log", "Per game", "Pin"],
         "unseen": ["Pinned players side by side (C)"]},
        {"file": "src/components/account/Connect.tsx", "desk": "account", "seen": ["Connect ESPN", "Connect", "Cookies from a signed-in ESPN tab"],
         "unseen": ["ESPN said the cookies have expired"]},
        {"file": "src/components/account/Alerts.tsx", "desk": "account", "seen": ["Email me before tip-off"], "unseen": []},
    ],
    "findings": [],
}


@pytest.fixture(scope="module")
def loaded():
    m = AppMapUpload.model_validate(MAP)
    return LoadedMap(build=BUILD, crawled_at=datetime(2026, 10, 20, 7, tzinfo=timezone.utc), map=m)


def ids(screens):
    return [s.id for s in screens]


def test_tokens_drop_stop_words_and_fold_plurals():
    assert tokens("Where can I find the free agents?") == ["find", "free", "agent"]
    assert tokens("Categories & stats") == ["category", "stat"]
    assert tokens("pass glass") == ["pass", "glass"]


@pytest.mark.parametrize("query,first", [
    ("free agents market pickup waivers", "week/market-p"),
    ("connect my espn league", "account/connect-espn"),
    ("email alerts before tip-off", "account/alerts"),
    ("player game log", "scout/pool-option"),
])
def test_a_question_finds_its_screen_first(loaded, query, first):
    assert ids(loaded.search(query))[0] == first


def test_crawled_data_is_not_searched(loaded):
    # The pool's first player is data, in a tree and in a trail: neither makes a screen match
    assert loaded.search("Jokic") == loaded.search("jokić") == []


def test_a_desk_narrows_the_search(loaded):
    assert {s.desk for s in loaded.search("connect espn market", desk="week")} == {"week"}
    assert loaded.search("connect espn", desk="scout") == []


def test_nothing_matches_nothing(loaded):
    assert loaded.search("") == []
    assert loaded.search("zzz qqq") == []


def test_copy_no_screen_showed_is_found_apart(loaded):
    unseen = loaded.search_unseen("compare players side by side")
    assert unseen[0] == {"desk": "scout", "file": "src/components/scout/PlayerSheet.tsx",
                         "copy": "Pinned players side by side (C)"}
    assert loaded.search_unseen("expired cookies", desk="week") == []


def test_find_gives_the_outline_screens_and_unseen_copy(loaded):
    r = loaded.find("free agents", requested_build=BUILD)
    assert r["build"] == BUILD and r["matches_build"] is True
    assert r["crawled_at"] == "2026-10-20T07:00:00+00:00"
    week = r["desks"][0]
    assert week == {"id": "week", "label": "Week", "href": "/week", "note": "Your team's matchup week",
                    "keys": "V view · P market", "needs": "account", "views": ["Market P", "PLAYERS"]}
    first = r["screens"][0]
    assert first == {"id": "week/market-p", "desk": "week", "steps": "Week › Market P", "link": "/week?market=1",
                     "signed_in": True, "tree": MAP["screens"][1]["tree"]}
    assert r["not_in_demo"][0]["copy"] == "No free agents fit this spot"


def test_find_from_another_build_says_so(loaded):
    assert loaded.find("market", requested_build="0" * 40)["matches_build"] is False
    assert loaded.find("market")["matches_build"] is False


def test_a_step_named_by_data_reads_as_one_of_a_list(loaded):
    r = loaded.find("game log")
    assert r["screens"][0]["steps"] == "Scout › 1 N. Jokić DEN C (one of a list)"


def test_a_long_tree_is_cut_at_a_line():
    long_tree = "\n".join(f"- button \"Market option {i}\"" for i in range(200))
    m = AppMapUpload.model_validate({**MAP, "screens": [screen("week/market-p", "week", [step("Market P")], long_tree)]})
    r = LoadedMap(build=BUILD, crawled_at=datetime(2026, 10, 20, tzinfo=timezone.utc), map=m).find("market")
    tree = r["screens"][0]["tree"]
    assert len(tree) <= TREE_CHARS + 4
    assert tree.endswith("\n- …")
    assert all(line.startswith("- ") for line in tree.splitlines())

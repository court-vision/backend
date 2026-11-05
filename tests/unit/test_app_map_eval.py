"""
The app map eval (evals/app_map): its cases, its grader, the free retrieval
check, and the end-to-end check's turn, run here on a scripted model.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.settings import settings
from evals.app_map import answers, grade, run
from schemas.app_map import AppMapUpload
from services.ai import agent
from services.ai.app_map import LoadedMap
from test_ai_agent import FakeMessages, msg, text, tool_use
from test_app_map_index import BUILD, MAP

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def loaded():
    return LoadedMap(build=BUILD, crawled_at=datetime(2026, 10, 20, 7, tzinfo=timezone.utc),
                     map=AppMapUpload.model_validate(MAP))


def test_the_cases_are_well_formed():
    cases = run.load_cases()
    assert grade.check_cases(cases) == []
    assert len(cases) >= 20
    assert any(c.get("absent") for c in cases)


def test_the_grader_passes_its_selfcheck():
    assert run.selfcheck(run.load_cases()) == 0


def test_a_bad_case_is_named():
    problems = grade.check_cases([{"id": "a", "question": "q"}, {"id": "a", "question": "q", "absent": True,
                                                                  "expect": [{"desk": "("}]}])
    assert any("either expect" in p for p in problems)
    assert any("duplicate id" in p for p in problems)


def test_retrieval_grades_each_findable_case(loaded):
    cases = [{"id": "market", "question": "Where do I pick up free agents?", "expect": [{"desk": "week", "steps": "Market"}]},
             {"id": "alerts", "question": "How do I get an email before tip-off?",
              "expect": [{"desk": "account", "steps": "alerts"}]},
             {"id": "trade", "question": "Can I trade?", "absent": True}]
    rows = run.retrieval(loaded, cases)
    assert [(r["id"], r["hit"]) for r in rows] == [("market", True), ("alerts", True)]


@pytest.fixture
def scripted(monkeypatch):
    def script(*replies):
        fake = FakeMessages(list(replies))
        monkeypatch.setattr(agent, "get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
        return fake
    monkeypatch.setattr(settings, "ai_agent_max_model_calls", 6)
    monkeypatch.setattr(settings, "ai_agent_turn_timeout_seconds", 5.0)
    return script


def ask(loaded, case):
    async def go():
        with answers._patches(loaded, None):
            return await answers.ask(loaded, case, "[Today is Thursday 2026-11-05 (US Eastern).]")
    return asyncio.run(go())


def test_a_case_runs_the_real_turn_on_the_eval_s_map(loaded, scripted):
    fake = scripted(
        msg("tool_use", tool_use("t1", "find_in_app", {"query": "free agents market pickup"}),
            tool_use("t2", "get_free_agents", {"team_id": 1})),
        msg("tool_use", tool_use("t3", "show_place", {"screen_id": "week/market-p"})),
        msg("end_turn", text("Open **Market P** on the Week desk.")),
    )
    case = {"id": "market", "question": "Where do I pick up a free agent?", "expect": [{"desk": "week", "steps": "Market"}]}

    outcome = ask(loaded, case)

    # find_in_app read the eval's map; the league lookup was turned away
    sent_back = fake.calls[1]["messages"][-1]["content"]
    found = json.loads(sent_back[0]["content"])
    assert found["build"] == BUILD and found["screens"][0]["id"] == "week/market-p"
    assert sent_back[1]["is_error"] and sent_back[1]["content"] == answers.OFF_LIMITS
    assert outcome["places"] == [{"id": "week/market-p", "desk": "week", "steps": "Week › Market P",
                                  "href": "/week?market=1"}]
    assert outcome["queries"] == ["free agents market pickup"]
    assert outcome["made_up"] == []
    assert grade.grade_answer(case, outcome)["pass"]


def test_a_made_up_place_fails_the_case(loaded, scripted):
    scripted(
        msg("tool_use", tool_use("t1", "find_in_app", {"query": "waivers"})),
        msg("end_turn", text("Open the **Waiver Wire** tab and claim him.")),
    )
    case = {"id": "waivers", "question": "Where are waivers?", "expect": [{"desk": "week", "steps": "Market"}]}

    outcome = ask(loaded, case)

    assert outcome["made_up"] == ["Waiver Wire"]
    graded = grade.grade_answer(case, outcome)
    assert not graded["pass"] and not graded["clean"] and not graded["right_place"]


def test_a_paid_check_needs_an_approved_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "FLOW", tmp_path)
    with pytest.raises(SystemExit, match="approve-harness"):
        run.main(["--answers", "--map", str(_map_file(tmp_path)), "--variant", "a1"])
    assert run.main(["--approve-harness"]) == 0
    assert run.approved()


def _map_file(tmp_path):
    path = tmp_path / "app-map.json"
    path.write_text(json.dumps(MAP))
    return path


def test_generated_questions_are_written_once_per_screen_and_searched(loaded, monkeypatch, tmp_path, capsys):
    from pydantic import SecretStr

    from evals.app_map import generated
    from services.ai import client

    prompts = []

    async def create(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(model="claude-haiku-5-5", content=[SimpleNamespace(
            type="text", text="Where do I find free agents to pick up?\n- How do I pick up a player?\nwhat else")],
            usage=SimpleNamespace(input_tokens=300, output_tokens=30, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-test"))
    out = tmp_path / "g1"
    out.mkdir()

    assert asyncio.run(generated.run(loaded, out, max_usd=1.0, model=None)) == 0
    assert len(prompts) == len(loaded.screens)
    assert "Steps: Week › Market P" in "\n".join(prompts)
    rows = [json.loads(line) for line in (out / f"generated-{BUILD[:12]}.jsonl").read_text().splitlines()]
    market = next(r for r in rows if r["id"] == "week/market-p")
    assert market["questions"][1] == "How do I pick up a player?" and market["ranks"][0] == 1

    # A second run reuses the questions: nothing is written again
    assert asyncio.run(generated.run(loaded, out, max_usd=1.0, model=None)) == 0
    assert len(prompts) == len(loaded.screens)

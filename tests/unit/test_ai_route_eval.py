"""
The routing eval's own correctness (evals/ai_route): a grader that passes a
wrong answer, or a runner that scores an API outage as a wrong answer, makes
every number the eval prints a lie.

What is pinned: the reference answer for every case passes and do-nothing
answers fail; the grader's idea of "the same place" matches the frontend's;
and the runner, driven through the real `AiService.route` by a scripted
client, writes a complete row for an answer, keeps a serving failure out of
the scores, and never lets a different model's answer be scored as this one's.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from core.errors import ProviderError
from core.settings import settings
from evals.ai_route import fixtures, grade, run
from schemas.ai import PageTarget, TerminalTarget
from services.ai import routing, service
from services.ai.routing import _Found
from services.scoring.category_rank import RANKABLE_KEYS

pytestmark = [pytest.mark.unit]

CASES = run.load_cases()
BY_ID = {case["id"]: case for case in CASES}
SENGUN, SABONIS, JOKIC, GIANNIS = 1630578, 1627734, 203999, 203507


def out(kind, **fields):
    return {"kind": kind, "text": "", "lookups": [], "ungrounded_numbers": 0, **fields}


def terminal(mode="player", **fields):
    return out("show", target={"type": "terminal", "mode": mode, "player_id": None, "compare_ids": [],
                               "team_id": None, "nba_team": None, "window": None, **fields})


def page(name, team_id=None, **rankings):
    return out("show", target={"type": "page", "page": name, "team_id": team_id,
                               "rankings": {"scope": None, "format": None, "window": None, "cats": [],
                                            "min_games": None, **rankings} if name == "rankings" else None})


def ok(case_id, answer):
    return grade.grade(BY_ID[case_id], answer)["route_ok"] == 1


class TestCases:
    def test_every_case_is_gradable_and_tagged(self):
        assert len(CASES) >= 75
        for case in CASES:
            assert case["question"].strip() and case["expect"], case["id"]
            assert case["tags"][0] in {"terminal", "rankings", "page", "statmuse", "cannot"}, case["id"]
            assert case.get("season", "preseason") in fixtures.SEASON_LINES, case["id"]

    def test_every_expected_destination_is_one_the_api_can_return(self):
        for case in CASES:
            answer = grade.oracle(case)
            target = answer["target"]
            if target is None:
                continue
            model = TerminalTarget if target["type"] == "terminal" else PageTarget
            model.model_validate(target)
            if target.get("rankings"):
                assert set(target["rankings"]["cats"]) <= set(RANKABLE_KEYS), case["id"]
            if target.get("team_id") is not None:
                assert target["team_id"] in fixtures.OWNED, case["id"]

    def test_fantasy_questions_never_list_statmuse_as_acceptable(self):
        for case in CASES:
            if "stay-home" in case["tags"]:
                assert all(alt["kind"] != "statmuse" for alt in case["expect"]), case["id"]


class TestKnownAnswers:
    def test_the_reference_answer_passes_every_case(self):
        failing = [c["id"] for c in CASES if grade.grade(c, grade.oracle(c))["route_ok"] != 1]
        assert failing == []

    @pytest.mark.parametrize("name", list(grade.NULLS))
    def test_a_do_nothing_answer_fails_almost_everywhere(self, name):
        passing = [c["id"] for c in CASES if grade.grade(c, grade.NULLS[name])["route_ok"] == 1]
        if name == "always cannot":  # right only where declining is the expected answer
            assert set(passing) <= {c["id"] for c in CASES if any(alt["kind"] == "cannot" for alt in c["expect"])}
        else:
            assert len(passing) <= 5

    def test_no_answer_scores_zero_on_every_metric(self):
        scores = grade.grade(BY_ID["ctx-last-10"], {"kind": None, "failure": "refusal"})
        assert set(scores.values()) == {0.0}
        assert "lean_lookups" in scores


class TestSamePlace:
    def test_a_page_without_a_team_is_the_selected_team(self):
        assert ok("matchup", page("matchup"))
        assert ok("matchup", page("matchup", team_id=101))
        assert not ok("matchup", page("matchup", team_id=102))

    def test_a_named_league_needs_that_team(self):
        assert ok("matchup-cats-league", page("matchup", team_id=102))
        assert not ok("matchup-cats-league", page("matchup"))          # would open the selected points team
        assert not ok("matchup-cats-league", page("matchup", team_id=101))

    def test_an_unnamed_window_keeps_what_is_showing(self):
        assert ok("player-plain", terminal(player_id=JOKIC))
        assert ok("player-plain", terminal(player_id=JOKIC, window="season"))
        assert not ok("player-plain", terminal(player_id=JOKIC, window="l10"))

    def test_a_named_window_is_exact(self):
        assert ok("player-last-15", terminal(player_id=SENGUN, window="l15"))
        assert not ok("player-last-15", terminal(player_id=SENGUN))
        # Asked for the full season while L10 is showing: null would leave L10 up
        assert ok("ctx-full-season", terminal(player_id=SENGUN, window="season"))
        assert not ok("ctx-full-season", terminal(player_id=SENGUN))

    def test_a_comparison_is_a_set_and_either_player_may_lead(self):
        assert ok("compare-two", terminal(player_id=SENGUN, compare_ids=[SABONIS]))
        assert ok("compare-two", terminal(player_id=SABONIS, compare_ids=[SENGUN]))
        assert not ok("compare-two", terminal(player_id=SENGUN))
        assert not ok("ctx-keep-compare", terminal(player_id=JOKIC, window="l30"))   # dropped the comparison
        assert not ok("player-plain", terminal(player_id=JOKIC, compare_ids=[GIANNIS]))  # added one unasked

    def test_the_wrong_mode_or_subject_fails(self):
        assert not ok("nba-schedule", terminal(mode="nba_team", nba_team="SAS"))
        assert not ok("nba-schedule", terminal(mode="overview"))
        assert not ok("my-other-team", terminal(mode="team", team_id=101))

    def test_rankings_follow_the_page(self):
        blocks = dict(format="categories", window=14, cats=["blk"])
        assert ok("rank-blocks-2-weeks", page("rankings", **blocks))
        # /rankings drops `cats` outside the categories format: this opens plain points rankings
        assert not ok("rank-blocks-2-weeks", page("rankings", **{**blocks, "format": None}))
        assert not ok("rank-blocks-2-weeks", page("rankings", **{**blocks, "window": None}))
        assert not ok("rank-blocks-2-weeks", page("rankings", **{**blocks, "cats": ["blk", "stl"]}))
        assert ok("rank-9cat-week", page("rankings", format="categories", window=7, cats=sorted(grade.STANDARD_9CAT)))
        assert ok("rank-nearest-window", page("rankings", window=7))
        assert ok("rank-nearest-window", page("rankings", window=14))
        assert not ok("rank-nearest-window", page("rankings"))
        assert not ok("rank-my-league", page("rankings"))
        assert ok("rank-my-league", page("rankings", scope="league"))
        assert not ok("rank-steals-min-games", page("rankings", format="categories", cats=["stl"]))

    def test_a_statmuse_question_is_checked_for_what_it_says(self):
        good = "Alperen Şengün and Domantas Sabonis rebounds per game 2025-26"
        assert ok("sm-compare-rebounds", out("statmuse", statmuse_query=good))
        for bad in ("Alperen Sengun vs Domantas Sabonis rebounds per game 2025-26",   # head-to-head games
                    "Alperen Sengun and Domantas Sabonis rebounds per game this season",
                    "Sengun and Sabonis rebounds per game 2025-26",                    # not full names
                    # everything it should say, plus the word that changes what StatMuse returns
                    "Alperen Sengun vs Domantas Sabonis rebounds and assists 2025-26",
                    "Kyle Anderson Alperen Sengun Domantas Sabonis rebounds 2025-26",  # "and" inside a name
                    ""):
            assert not ok("sm-compare-rebounds", out("statmuse", statmuse_query=bad)), bad
        assert ok("sm-head-to-head", out("statmuse", statmuse_query="Kevin Durant vs LeBron James record"))
        assert not ok("sm-compare-rebounds", terminal(player_id=SENGUN, compare_ids=[SABONIS]))

    def test_cannot_is_the_model_declining_not_the_server_refusing(self):
        assert ok("no-odds", out("cannot", gap="no_data"))
        assert not ok("no-odds", out("cannot", gap="invalid_target"))
        assert not ok("no-weather", out("cannot", gap="no_data"))       # must be out_of_scope
        assert grade.grade(BY_ID["player-plain"], out("cannot", gap="invalid_target"))["kind_ok"] == 0

    def test_a_made_up_number_or_a_second_line_fails_clean_text(self):
        case = BY_ID["matchup"]
        assert grade.grade(case, page("matchup"))["clean_text"] == 1
        assert grade.grade(case, {**page("matchup"), "ungrounded_numbers": 1})["clean_text"] == 0
        assert grade.grade(case, {**page("matchup"), "text": "Opening it.\nYou lead 512-498."})["clean_text"] == 0

    def test_every_case_says_how_many_lookups_its_answer_can_need(self):
        assert all(isinstance(case.get("max_lookups"), int) for case in CASES)

    def test_a_lookup_the_screen_made_unnecessary_is_a_wasted_trip(self):
        searched = {**terminal(player_id=1628983, window="l10"), "lookups": [{"name": "search_players"}], "model_calls": 2}
        assert grade.grade(BY_ID["ctx-last-10"], searched) == {
            "route_ok": 1.0, "kind_ok": 1.0, "clean_text": 1.0, "lean_lookups": 0.0}
        # No lookup, but a second trip to the model anyway
        assert grade.grade(BY_ID["playoffs"], {**page("playoffs"), "model_calls": 2})["lean_lookups"] == 0
        assert grade.grade(BY_ID["playoffs"], {**page("playoffs"), "model_calls": 1})["lean_lookups"] == 1

    def test_needed_lookups_are_made_together(self):
        durant = {**terminal(mode="nba_team", nba_team="HOU"), "lookups": [{}]}
        assert grade.grade(BY_ID["nba-traded-player"], {**durant, "model_calls": 2})["lean_lookups"] == 1
        assert grade.grade(BY_ID["nba-traded-player"], {**durant, "model_calls": 3})["lean_lookups"] == 0   # a trip too many
        assert grade.grade(BY_ID["nba-traded-player"], {**durant, "lookups": [{}, {}], "model_calls": 2})["lean_lookups"] == 0

    def test_a_player_needs_no_lookup_because_the_server_finds_him(self):
        both = terminal(player_id=SENGUN, compare_ids=[SABONIS])
        assert grade.grade(BY_ID["compare-two"], {**both, "model_calls": 1})["lean_lookups"] == 1
        searched = {**both, "lookups": [{}, {}], "model_calls": 2}
        assert grade.grade(BY_ID["compare-two"], searched)["lean_lookups"] == 0
        assert grade.grade(BY_ID["compare-two"], searched)["route_ok"] == 1    # speed never changes the routing score

    def test_a_fantasy_rankings_question_may_open_in_my_leagues_scoring(self):
        assert ok("rank-points-month", page("rankings", format="points", window=30, scope="league", team_id=101))
        assert ok("rank-points-month", page("rankings", format="points", window=30))
        assert ok("rank-top-20", page("rankings", scope="league"))
        assert not ok("rank-blocks-2-weeks", page("rankings", format="categories", window=14, cats=["blk"], scope="league"))


class TestSummary:
    def test_numbers_are_recomputed_per_case_and_exclude_truncated_rows(self):
        cases = [BY_ID["matchup"], BY_ID["sm-by-season"], BY_ID["no-odds"]]

        def row(case_id, answer, rep=0, status="ok"):
            return {"prompt_id": case_id, "rep": rep, "status": status, "output": answer,
                    "grade": grade.grade(BY_ID[case_id], answer)}
        rows = [
            row("matchup", page("matchup")),
            row("matchup", out("statmuse", statmuse_query="my matchup"), rep=1),   # a leak on one of two reps
            row("sm-by-season", out("cannot", gap="no_data")),
            row("no-odds", out("cannot", gap="no_data")),
            row("no-odds", out("cannot", gap="no_data"), rep=1, status="truncated"),
        ]
        s = grade.summarize(rows, cases)

        assert (s["cases"], s["rows"], s["truncated"]) == (3, 4, 1)
        assert s["metrics"]["route_ok"]["rate"] == pytest.approx((0.5 + 0 + 1) / 3)
        assert s["confusion"]["statmuse"] == {"precision": 0.0, "recall": 0.0, "said": 1, "wanted": 1}
        assert s["confusion"]["cannot"]["precision"] == 0.5
        assert s["stay_home"] == {"n": 2, "leaks": ["matchup"]}
        assert s["failed"] == ["matchup", "sm-by-season"]
        assert s["unquoted"]["n"] == 3   # none of these three is quoted in the prompt

    def test_wilson_interval_is_sane_at_the_edges(self):
        low, high = grade.wilson(92, 92)
        assert 0.95 < low < 1 and high == 1.0
        assert grade.wilson(0, 0) == (0.0, 0.0)


# ------------------------------------------------- the runner, end to end


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def msg(stop_reason, *content, model="claude-opus-5"):
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(content), model=model, _request_id="req_1",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=3000,
                              cache_creation_input_tokens=0, iterations=None),
    )


def answer(kind, text_, target=None, statmuse_query=None, gap=None):
    return text(json.dumps({"kind": kind, "text": text_, "target": target, "statmuse_query": statmuse_query,
                            "suggestions": [], "gap": gap, "missing": None}))


MATCHUP_102 = {"type": "page", "page": "matchup", "team_id": 102, "rankings": None}


@pytest.fixture
def scripted(monkeypatch):
    """The real route, loop, tools and validation; a scripted model; no database."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-test"))
    monkeypatch.setattr(settings, "ai_model", "claude-opus-5")
    monkeypatch.setattr(settings, "ai_request_timeout_seconds", 5.0)
    calls = []

    async def no_db_lookup(player_ids, team_ids, nba_teams, user_id):
        return _Found(frozenset(), frozenset(), frozenset())
    monkeypatch.setattr(routing, "_lookup", no_db_lookup)

    async def no_db_names(player_ids, nba_team):  # the view then carries IDs without names
        return {}, None
    monkeypatch.setattr(routing, "_view_names", no_db_names)
    fixtures.install(monkeypatch.setattr)

    def install(*steps):
        script = list(steps)

        async def create(**kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        monkeypatch.setattr(service, "get_client",
                            lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
        return calls
    return install


def attempt(case_id):
    return asyncio.run(run.attempt(BY_ID[case_id], 0, model="claude-opus-5", timeout_s=5, attempt_no=1))


class TestRunner:
    def test_an_answer_becomes_a_complete_graded_row(self, scripted):
        team_101 = {"type": "terminal", "mode": "team", "player": None, "compare": [], "team_id": 101,
                    "nba_team": None, "window": None}
        calls = scripted(
            msg("tool_use", tool_use("t1", "get_my_teams", {})),
            msg("end_turn", answer("show", "Opening Area 51 Ballers", team_101)),
        )

        kind, row, trace = attempt("my-team-by-name")

        assert kind == "row"
        assert row["grade"] == {"route_ok": 1.0, "kind_ok": 1.0, "clean_text": 1.0, "lean_lookups": 1.0}
        assert (row["model"], row["model_calls"], row["tool_calls"], row["status"]) == ("claude-opus-5", 2, 1, "ok")
        assert row["usage"] == {"input_tokens": 200, "output_tokens": 40, "cache_read_input_tokens": 6000,
                                "cache_creation_input_tokens": 0}
        assert row["output"]["target"]["team_id"] == 101          # ownership came from the fixture
        assert row["output"]["ungrounded_numbers"] == 0            # the 51 is in the team's name
        assert [turn["role"] for turn in trace] == ["system", "user", "tool_call", "tool_result", "assistant"]
        assert "Splash Cousins" in trace[3]["content"]             # the fixture's teams reached the model
        turn = calls[0]["messages"][0]["content"]
        assert turn.startswith(fixtures.SEASON_LINES["preseason"])
        # ... and the view lists them by format, without their names
        assert '"scoring": "categories"' in turn and "Splash Cousins" not in turn

    def test_a_described_league_is_answered_from_the_view_in_one_trip(self, scripted):
        scripted(msg("end_turn", answer("show", "Opening your categories league matchup", MATCHUP_102)))

        kind, row, _ = attempt("matchup-cats-league")

        assert (kind, row["model_calls"], row["tool_calls"]) == ("row", 1, 0)
        assert row["grade"] == {"route_ok": 1.0, "kind_ok": 1.0, "clean_text": 1.0, "lean_lookups": 1.0}

    def test_a_case_can_put_the_season_under_way(self, scripted):
        calls = scripted(msg("end_turn", answer(
            "statmuse", "Asking StatMuse", statmuse_query="Jayson Tatum and Jaylen Brown rebounds per game 2026-27",
            gap="no_view")))

        kind, row, _ = attempt("sm-in-season")

        assert calls[0]["messages"][0]["content"].startswith("NBA season: 2026-27, in progress.")
        assert (kind, row["grade"]["route_ok"], row["grade"]["lean_lookups"]) == ("row", 1.0, 1.0)

    def test_a_team_the_fixture_user_does_not_own_is_refused_and_fails(self, scripted):
        scripted(msg("end_turn", answer("show", "Opening your matchup", {**MATCHUP_102, "team_id": 15})))

        kind, row, _ = attempt("matchup")

        assert kind == "row"
        assert (row["output"]["kind"], row["output"]["gap"]) == ("cannot", "invalid_target")
        assert row["output"]["rejected_target"]["team_id"] == 15
        assert row["grade"]["route_ok"] == 0

    def test_a_serving_failure_is_an_error_not_a_score(self, scripted):
        scripted(ProviderError("anthropic", "down", error_code="AI_UNAVAILABLE"))

        kind, record, _ = attempt("matchup")

        assert kind == "error"
        assert (record["class"], record["code"]) == ("serving_error", "AI_UNAVAILABLE")
        assert "grade" not in record

    def test_a_refusal_is_a_graded_zero(self, scripted):
        scripted(msg("refusal"))

        kind, row, _ = attempt("matchup")

        assert kind == "row"
        assert row["output"] == {"kind": None, "failure": "refusal", "lookups": [], "model_calls": 1}
        assert set(row["grade"].values()) == {0.0}

    def test_an_answer_from_another_model_is_not_scored(self, scripted):
        scripted(msg("end_turn", answer("show", "Opening your matchup", {**MATCHUP_102, "team_id": None}),
                     model="claude-opus-5-5"))

        kind, record, _ = attempt("matchup")

        assert (kind, record["class"]) == ("error", "served_model_mismatch")

    def test_regrading_keeps_the_answers_and_drops_a_row_for_a_reworded_question(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(run, "FLOW", tmp_path)
        (tmp_path / "baseline" / "traces").mkdir(parents=True)

        def stored(case_id, answer, prompt=None, grade_=None):
            (tmp_path / "baseline" / "traces" / f"{case_id}_rep0.json").write_text("[]")
            return {"prompt_id": case_id, "rep": 0, "prompt": prompt or run.prompt_text(BY_ID[case_id]), "tags": [],
                    "model_calls": 1, "output": answer, "grade": grade_ or {"route_ok": 0.0}}
        league = page("rankings", format="points", window=30, scope="league", team_id=101)
        rows = [stored("rank-points-month", league),                              # graded wrong under the old case
                {**stored("playoffs", page("playoffs")), "model_calls": 2},       # right place, one trip too many
                stored("sm-in-season", terminal(player_id=1628369), prompt="which of these two scores more?")]
        (tmp_path / "baseline" / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

        run.regrade("baseline", CASES)

        kept = run.read_jsonl(tmp_path / "baseline" / "results.jsonl")
        assert [r["prompt_id"] for r in kept] == ["rank-points-month", "playoffs"]
        assert kept[1]["grade"] == {"route_ok": 1.0, "kind_ok": 1.0, "clean_text": 1.0, "lean_lookups": 0.0}
        assert kept[0]["output"] == league and kept[0]["grade"]["route_ok"] == 1.0
        assert kept[0]["grade"]["lean_lookups"] == 1.0 and kept[0]["tags"] == BY_ID["rank-points-month"]["tags"]
        assert not (tmp_path / "baseline" / "traces" / "sm-in-season_rep0.json").exists()
        assert "dropped 1 ['sm-in-season']" in capsys.readouterr().out

    def test_the_bars_judge_routing_and_time_separately(self):
        bars = {"route_ok_min": 0.9, "leaks_max": 0, "made_up_max": 0, "refused_max": 0,
                "latency_p50_s": 3.0, "latency_p90_s": 6.0}
        summary = {"metrics": {"route_ok": {"rate": 0.95, "n": 20}, "clean_text": {"rate": 0.95, "n": 20}},
                   "stay_home": {"leaks": []}, "invalid_targets": ["x"]}
        verdicts = {name: passed for name, passed, _ in run.check_bars(bars, summary, [2.0] * 8 + [7.0, 9.0])}
        assert verdicts == {"right place": True, "fantasy questions sent to StatMuse": True, "made-up numbers": False,
                            "refused destinations": False, "time, half within": True, "time, 9 in 10 within": False}
        assert run.check_bars({}, summary, [1.0]) == []

    def test_a_dated_snapshot_of_the_model_is_the_same_model(self):
        assert run.served_as_asked("claude-opus-5", "claude-opus-5")
        assert run.served_as_asked("claude-opus-5", "claude-opus-5-20260714")
        assert not run.served_as_asked("claude-opus-5", "claude-opus-5-5")

    def test_cost_prices_every_meter_and_the_harness_hash_follows_its_files(self, tmp_path, monkeypatch):
        prices = {"claude-opus-5": {"in": 5.0, "out": 25.0}}
        row = {"model": "claude-opus-5-20260714", "requested_model": "claude-opus-5",
               "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 4000,
                         "cache_creation_input_tokens": 2000}}
        assert run.cost_usd(row, prices) == pytest.approx((1000 * 5 + 100 * 25 + 2000 * 6.25 + 4000 * 0.5) / 1e6)
        # A model with its own cache rates (Opus 5.5 reads at a twentieth of input, not a tenth)
        own = {"claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_write": 5.0, "cache_read": 0.20}}
        assert run.cost_usd({**row, "model": "claude-opus-5-5"}, own) == pytest.approx(
            (1000 * 4 + 100 * 20 + 2000 * 5.0 + 4000 * 0.20) / 1e6)

        (tmp_path / "a.py").write_text("one")
        monkeypatch.setattr(run, "ROOT", tmp_path)
        before = run.harness_sha({"harness_paths": ["a.py"]})
        (tmp_path / "a.py").write_text("two")
        assert run.harness_sha({"harness_paths": ["a.py"]}) != before

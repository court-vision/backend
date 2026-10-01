"""
The router's answer contract (services/ai/routing.py): the schema the model
fills, the StatMuse link the server builds, and the checks that decide whether
a destination reaches the client.

No database: `_check` is pure over what `_lookup` found, and `validate` is
tested with `_lookup` stubbed. The migration and real lookups are covered in
tests/integration/test_ai_questions_integration.py.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.errors import ProviderError
from schemas.ai import PageTarget, RankingsParams, TerminalTarget
from services.ai import routing
from services.ai.routing import RouterAnswer, _Found
from services.scoring.category_rank import RANKABLE_KEYS

SENGUN, SABONIS, JOKIC, UNKNOWN = 1630578, 1627734, 203999, 999
MY_TEAM, NOT_MY_TEAM = 7, 8
FOUND = _Found(players=frozenset({SENGUN, SABONIS, JOKIC}), owned_teams=frozenset({MY_TEAM}),
               nba_teams=frozenset({"HOU", "SAC"}))


def show(target, text="Opening it"):
    return RouterAnswer(kind="show", text=text, target=target)


def player(pid=SENGUN, compare=(), window=None):
    return TerminalTarget(mode="player", player_id=pid, compare_ids=list(compare), window=window)


def terminal_answer(kind="show", text="Opening Sengun", window=None, statmuse_query=None):
    """The model's JSON for a player target, every field said as ANSWER_SCHEMA requires."""
    return {"kind": kind, "text": text, "statmuse_query": statmuse_query, "gap": None, "missing": None,
            "suggestions": [],
            "target": {"type": "terminal", "mode": "player", "player_id": SENGUN, "compare_ids": [],
                       "team_id": None, "nba_team": None, "window": window}}


def rankings_answer(page, min_games):
    return {"kind": "show", "text": "Opening it", "statmuse_query": None, "gap": None, "missing": None,
            "suggestions": [],
            "target": {"type": "page", "page": page, "team_id": None,
                       "rankings": {"scope": None, "format": None, "window": 14, "cats": ["blk"],
                                    "min_games": min_games}}}


def walk(schema):
    """Every dict node in a JSON schema."""
    if isinstance(schema, dict):
        yield schema
        for value in schema.values():
            yield from walk(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from walk(item)


@pytest.mark.unit
class TestSchema:
    def test_every_object_is_closed_and_fully_required(self):
        for node in walk(routing.ANSWER_SCHEMA):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])

    def test_uses_no_constraint_structured_outputs_rejects(self):
        unsupported = {"minLength", "maxLength", "minimum", "maximum", "multipleOf", "minItems", "maxItems"}
        for node in walk(routing.ANSWER_SCHEMA):
            assert not unsupported & set(node), node

    def test_the_model_cannot_claim_the_servers_gap(self):
        gap_enum = routing.ANSWER_SCHEMA["properties"]["gap"]["anyOf"][0]["enum"]
        assert "invalid_target" not in gap_enum
        assert set(gap_enum) == {"no_view", "no_data", "out_of_scope"}

    def test_category_keys_follow_the_rankings_service(self):
        cats = routing.ANSWER_SCHEMA["$defs"]["rankings"]["properties"]["cats"]["items"]["enum"]
        assert cats == list(RANKABLE_KEYS)

    def test_the_format_is_byte_stable(self):
        """It rides on every call; a changing byte would change the cached prefix."""
        assert json.dumps(routing.ANSWER_FORMAT) == json.dumps(routing.ANSWER_FORMAT)
        assert routing.ANSWER_FORMAT["type"] == "json_schema"


@pytest.mark.unit
class TestStatmuseUrl:
    def test_the_shape_statmuse_answers(self):
        """Verified 2026-09-30: this exact slug answers "9.2 rebounds per game"."""
        assert routing.statmuse_url("Alperen Şengün rebounds per game, last 15 games") == (
            "https://www.statmuse.com/nba/ask/alperen-sengun-rebounds-per-game-last-15-games")

    @pytest.mark.parametrize("query", [
        "https://evil.example/phish?x=1",
        "../../../admin",
        "javascript:alert(1)",
        "Jokic\ncareer triple doubles\r\n<script>",
    ])
    def test_whatever_the_model_writes_the_link_stays_a_statmuse_search(self, query):
        url = routing.statmuse_url(query)
        assert url.startswith("https://www.statmuse.com/nba/ask/")
        slug = url.removeprefix("https://www.statmuse.com/nba/ask/")
        assert slug and all(c.isalnum() or c == "-" for c in slug)

    def test_nothing_searchable_is_no_link(self):
        assert routing.statmuse_url("?!… ✓") is None

    def test_long_questions_are_capped(self):
        slug = routing.statmuse_url("rebounds " * 100).removeprefix(routing.STATMUSE_ASK)
        assert len(slug) <= routing.MAX_QUERY and not slug.endswith("-")


@pytest.mark.unit
class TestUngroundedNumbers:
    def test_numbers_from_the_question_are_fine(self):
        assert routing.ungrounded_numbers("Opening Sengun's last 15 games", "sengun last 15", None) == 0

    def test_numbers_from_the_target_are_fine(self):
        assert routing.ungrounded_numbers("Showing the last 20", "how's he been", player(window="l20")) == 0

    def test_a_statistic_the_router_made_up_is_counted(self):
        assert routing.ungrounded_numbers("He's averaging 21.4 points", "how's sengun", player()) == 1

    def test_numbers_inside_names_are_not_statistics(self):
        text = "Opening the matchup for your Lvl. 3 Goblins team in Dorm League 2"
        assert routing.ungrounded_numbers(text, "am I winning", None) == 2
        assert routing.ungrounded_numbers(text, "am I winning", None, ["lvl. 3 goblins", "Dorm League 2"]) == 0

    def test_removing_names_does_not_hide_a_made_up_stat(self):
        text = "Lvl. 3 Goblins lead by 12.5"
        assert routing.ungrounded_numbers(text, "am I winning", None, ["Lvl. 3 Goblins"]) == 1


@pytest.mark.unit
def test_describe_view_names_players_and_nba_teams_but_not_fantasy_teams(monkeypatch):
    from schemas.ai import AiContext
    seen = []

    async def fake(player_ids, nba_team):
        seen.append((player_ids, nba_team))
        return {SENGUN: "Alperen Sengun"}, "Houston Rockets"
    monkeypatch.setattr(routing, "_view_names", fake)

    view = asyncio.run(routing.describe_view(AiContext(
        page="terminal", mode="player", player_id=SENGUN, compare_ids=[UNKNOWN], team_id=7, nba_team="HOU",
        window="l15")))

    assert seen == [([SENGUN, UNKNOWN], "HOU")]
    assert view == {
        "page": "terminal", "mode": "player", "team_id": 7, "window": "l15",
        "player": {"id": SENGUN, "name": "Alperen Sengun"},
        "compare": [{"id": UNKNOWN, "name": None}],
        "nba_team": {"abbrev": "HOU", "name": "Houston Rockets"},
    }


@pytest.mark.unit
def test_an_empty_view_needs_no_lookup(monkeypatch):
    from schemas.ai import AiContext

    async def fail(*args):
        raise AssertionError("no lookup for an empty view")
    monkeypatch.setattr(routing, "_view_names", fail)

    assert asyncio.run(routing.describe_view(AiContext(page="rankings"))) == {"page": "rankings"}


@pytest.mark.unit
class TestParse:
    def test_reads_the_final_json(self):
        answer = routing.parse_answer(json.dumps({
            "kind": "show", "text": " Opening Sengun ", "statmuse_query": None, "gap": None, "missing": None,
            "suggestions": [],
            "target": {"type": "terminal", "mode": "player", "player_id": SENGUN, "compare_ids": [],
                       "team_id": None, "nba_team": None, "window": "l15"},
        }))
        assert isinstance(answer.target, TerminalTarget)
        assert (answer.text, answer.target.window) == ("Opening Sengun", "l15")

    def test_keeps_at_most_two_suggestions(self):
        answer = routing.parse_answer(json.dumps({
            "kind": "cannot", "text": "No news here", "target": None, "statmuse_query": None, "gap": "no_data",
            "missing": "news feed", "suggestions": ["a", " ", "b", "c"],
        }))
        assert answer.suggestions == ["a", "b"]

    @pytest.mark.parametrize("raw", ["", "not json", "[]", '{"kind": "maybe", "text": "x"}',
                                     '{"kind": "show", "text": "x", "target": "the terminal"}',
                                     '{"kind": "maybe", "text": "x", "target": {"type": "terminal", "mode": "player", "window": "l99"}}'])
    def test_an_unreadable_answer_is_the_models_failure(self, raw):
        with pytest.raises(ProviderError) as exc:
            routing.parse_answer(raw)
        assert exc.value.error_code == "AI_INCOMPLETE"

    def test_an_unreadable_answer_logs_the_fields_that_failed(self, monkeypatch):
        logged = []
        monkeypatch.setattr(routing, "get_logger", lambda: SimpleNamespace(
            warning=lambda event, **kw: logged.append((event, kw))))

        for raw in ("not json", json.dumps({**terminal_answer(window="l99"), "text": None, "gap": "invalid_target?"})):
            with pytest.raises(ProviderError):
                routing.parse_answer(raw)

        assert logged == [
            ("ai_route_unparseable", {"error": "JSONDecodeError", "fields": []}),
            ("ai_route_unparseable", {"error": "ValidationError",
                                      "fields": ["text", "target.terminal.window", "gap"]}),
        ]

    @pytest.mark.parametrize("written, window", [("L15", "l15"), (" l15 ", "l15"), ("Season", "season"), ("", None)])
    def test_a_window_the_schema_could_not_constrain_is_tidied(self, written, window):
        assert routing.parse_answer(json.dumps(terminal_answer(window=written))).target.window == window

    @pytest.mark.parametrize("window", ["l99", "l100", "l0", "15", "last 15"])
    def test_a_window_out_of_range_is_an_invalid_target_not_an_unreadable_answer(self, window):
        """It was a 502 "try again" that spent the quota and left the log row empty."""
        answer = routing.parse_answer(json.dumps(terminal_answer(window=window)))

        assert (answer.kind, answer.gap, answer.target) == ("cannot", "invalid_target", None)
        assert answer.text == routing.INVALID_TARGET_TEXT
        assert answer.missing == "rejected: target.terminal.window"
        assert answer.rejected_target["window"] == window

    @pytest.mark.parametrize("min_games, kept", [(0, None), (-1, None), (83, None), (100, None), (1, 1), (82, 82)])
    def test_a_games_minimum_out_of_range_is_no_minimum(self, min_games, kept):
        answer = routing.parse_answer(json.dumps(rankings_answer("rankings", min_games)))
        assert answer.kind == "show" and answer.target.rankings.min_games == kept

    def test_rankings_params_on_another_page_cannot_sink_the_answer(self):
        answer = routing.parse_answer(json.dumps(rankings_answer("streamers", 0)))
        assert answer.kind == "show" and answer.target.page == "streamers"

    def test_only_a_show_has_a_target_to_fail(self):
        raw = terminal_answer(kind="statmuse", window="l100", statmuse_query="Nikola Jokic career triple doubles")
        answer = routing.parse_answer(json.dumps(raw))
        assert (answer.kind, answer.target, answer.gap) == ("statmuse", None, None)

    def test_the_model_cannot_write_the_rejected_target(self):
        raw = {**terminal_answer(), "rejected_target": {"x": 1}, "_rejected_target": {"x": 1}}
        assert routing.parse_answer(json.dumps(raw)).rejected_target is None


@pytest.mark.unit
class TestCheckTerminal:
    def test_a_valid_player_target_passes(self):
        checked = routing._check(show(player(window="l15")), FOUND)
        assert checked.kind == "show" and checked.target.player_id == SENGUN and checked.target.window == "l15"

    def test_an_unknown_player_is_a_bug_not_an_answer(self):
        checked = routing._check(show(player(UNKNOWN)), FOUND)
        assert (checked.kind, checked.gap, checked.target) == ("cannot", "invalid_target", None)

    def test_a_refused_target_is_kept_for_the_log_with_the_reason(self):
        """The row said only `invalid_target`: not which ID the model proposed, nor why it failed."""
        checked = routing._check(show(player(UNKNOWN, window="l15")), FOUND)
        assert checked.missing == "rejected: player_id"
        assert checked.rejected_target == player(UNKNOWN, window="l15").model_dump()
        assert "rejected_target" not in checked.model_dump() and "_rejected_target" not in checked.model_dump()

        theirs = routing._check(show(PageTarget(page="matchup", team_id=NOT_MY_TEAM)), FOUND)
        assert (theirs.missing, theirs.rejected_target["team_id"]) == ("rejected: team_id", NOT_MY_TEAM)

    def test_an_answer_that_passes_carries_no_rejected_target(self):
        checked = routing._check(show(player()), FOUND)
        assert (checked.missing, checked.rejected_target) == (None, None)

    def test_the_comparison_drops_duplicates_and_the_focused_player(self):
        checked = routing._check(show(player(compare=[SABONIS, SENGUN, SABONIS, JOKIC])), FOUND)
        assert checked.target.compare_ids == [SABONIS, JOKIC]

    def test_an_unknown_player_in_the_comparison_is_invalid(self):
        assert routing._check(show(player(compare=[SABONIS, UNKNOWN])), FOUND).gap == "invalid_target"

    def test_more_than_four_to_compare_is_invalid(self):
        found = _Found(players=frozenset(range(1, 10)), owned_teams=frozenset(), nba_teams=frozenset())
        assert routing._check(show(player(1, compare=[2, 3, 4, 5, 6])), found).gap == "invalid_target"

    def test_team_mode_needs_one_of_the_callers_teams(self):
        mine = routing._check(show(TerminalTarget(mode="team", team_id=MY_TEAM)), FOUND)
        theirs = routing._check(show(TerminalTarget(mode="team", team_id=NOT_MY_TEAM)), FOUND)
        assert mine.target.team_id == MY_TEAM
        assert (theirs.kind, theirs.gap) == ("cannot", "invalid_target")

    def test_nba_team_is_normalized_and_checked(self):
        assert routing._check(show(TerminalTarget(mode="nba_team", nba_team="hou")), FOUND).target.nba_team == "HOU"
        assert routing._check(show(TerminalTarget(mode="nba_team", nba_team="XYZ")), FOUND).gap == "invalid_target"

    def test_fields_other_modes_cannot_use_are_cleared(self):
        """The client sets all three focus fields from the target; stray ones would win."""
        target = TerminalTarget(mode="team", team_id=MY_TEAM, player_id=SENGUN, compare_ids=[SABONIS], nba_team="HOU")
        checked = routing._check(show(target), FOUND).target
        assert (checked.player_id, checked.compare_ids, checked.nba_team) == (None, [], None)
        overview = routing._check(show(TerminalTarget(mode="overview", player_id=SENGUN)), FOUND).target
        assert overview.player_id is None


@pytest.mark.unit
class TestCheckPageAndOthers:
    def test_rankings_keep_their_params(self):
        target = PageTarget(page="rankings", rankings=RankingsParams(window=14, cats=["blk"], format="categories"))
        checked = routing._check(show(target), FOUND).target
        assert checked.rankings.window == 14 and checked.rankings.cats == ["blk"]

    def test_rankings_params_on_another_page_are_dropped(self):
        target = PageTarget(page="streamers", team_id=MY_TEAM, rankings=RankingsParams(window=7))
        assert routing._check(show(target), FOUND).target.rankings is None

    def test_a_page_for_someone_elses_team_is_invalid(self):
        assert routing._check(show(PageTarget(page="matchup", team_id=NOT_MY_TEAM)), FOUND).gap == "invalid_target"

    def test_show_needs_a_target(self):
        assert routing._check(show(None), FOUND).gap == "invalid_target"

    def test_statmuse_needs_a_question_and_carries_no_target(self):
        good = routing._check(RouterAnswer(kind="statmuse", text="t", statmuse_query=" Jokic career triple doubles ",
                                           target=player()), FOUND)
        bad = routing._check(RouterAnswer(kind="statmuse", text="t", statmuse_query="  "), FOUND)
        assert (good.kind, good.target, good.statmuse_query) == ("statmuse", None, "Jokic career triple doubles")
        assert (bad.kind, bad.gap) == ("cannot", "invalid_target")

    def test_cannot_carries_no_destination(self):
        checked = routing._check(RouterAnswer(kind="cannot", text="No news", target=player(),
                                              statmuse_query="x", gap="no_data"), FOUND)
        assert (checked.target, checked.statmuse_query, checked.gap) == (None, None, "no_data")


@pytest.mark.unit
def test_validate_checks_every_id_in_one_lookup_scoped_to_the_caller(monkeypatch):
    seen = []

    async def fake_lookup(player_ids, team_ids, nba_teams, user_id):
        seen.append((sorted(player_ids), team_ids, nba_teams, user_id))
        return FOUND
    monkeypatch.setattr(routing, "_lookup", fake_lookup)

    asyncio.run(routing.validate(show(player(compare=[SABONIS])), user_id=42))
    asyncio.run(routing.validate(show(PageTarget(page="matchup", team_id=MY_TEAM)), user_id=42))
    asyncio.run(routing.validate(RouterAnswer(kind="cannot", text="x", target=player()), user_id=42))

    assert seen == [
        ([SABONIS, SENGUN], [], [], 42),
        ([], [MY_TEAM], [], 42),
        ([], [], [], 42),  # a cannot's stray target is never looked up
    ]

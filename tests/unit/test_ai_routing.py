"""
The router's answer contract (services/ai/routing.py): the schema the model
fills, the StatMuse link the server builds, and the checks that decide whether
a destination reaches the client.

No database: `_check` is pure over what `_lookup` and `_find_players` found, and
`validate` is tested with both stubbed. The migration and real lookups are covered in
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


# What `_find_players` would return for each name the model might give: (id, name), best first
PLAYERS = {"Alperen Sengun": [(SENGUN, "Alperen Sengun")], "Domantas Sabonis": [(SABONIS, "Domantas Sabonis")],
           "Nikola Jokic": [(JOKIC, "Nikola Jokić")],
           "Thompson": [(1641708, "Amen Thompson"), (1641709, "Ausar Thompson"), (202691, "Klay Thompson")],
           "Nobody Atall": []}


def show(target, text="Opening it"):
    return RouterAnswer(kind="show", text=text, target=target)


def named(name="Alperen Sengun", compare=(), window=None, mode="player", **fields):
    """A parsed answer for a terminal target, carrying the player names the model gave."""
    answer = show(TerminalTarget(mode=mode, window=window, **fields))
    answer._player, answer._compare = name, list(compare)
    return answer


def check(answer, found=FOUND, players=PLAYERS):
    return routing._check(answer, found, players)


def player(pid=SENGUN, compare=(), window=None):
    return TerminalTarget(mode="player", player_id=pid, compare_ids=list(compare), window=window)


def terminal_answer(kind="show", text="Opening Sengun", window=None, statmuse_query=None):
    """The model's JSON for a player target, every field said as ANSWER_SCHEMA requires."""
    return {"kind": kind, "text": text, "statmuse_query": statmuse_query, "gap": None, "missing": None,
            "suggestions": [],
            "target": {"type": "terminal", "mode": "player", "player": "Alperen Sengun", "compare": [],
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
        assert "invalid_target" not in gap_enum and "ambiguous" not in gap_enum
        assert set(gap_enum) == {"no_view", "no_data", "out_of_scope"}

    def test_the_model_names_players_and_never_writes_an_id(self):
        terminal = routing.ANSWER_SCHEMA["$defs"]["terminal"]["properties"]
        assert terminal["player"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
        assert terminal["compare"] == {"type": "array", "items": {"type": "string"}}
        assert "player_id" not in terminal and "compare_ids" not in terminal

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

    def test_a_name_that_is_a_digit_is_not_cut_out_of_a_longer_number(self):
        """A league called "1" turned the question's own 15 into a made-up 5."""
        assert routing.ungrounded_numbers("Opening last 15 games", "last 15 games", None, ["1"]) == 0
        assert routing.ungrounded_numbers("Opening 1, last 15 games", "last 15 games", None, ["1"]) == 0
        assert routing.ungrounded_numbers("Up 21 in 1", "am I winning", None, ["1"]) == 1

    SEASON_LINE = "NBA season: 2026-27 starts 2026-10-20; the latest season with games is 2025-26."

    def test_a_season_the_request_named_is_not_a_statistic(self):
        """Every StatMuse answer that named its season was logged as two made-up numbers."""
        text = "Checking their assists per game for 2025-26 on StatMuse"
        assert routing.ungrounded_numbers(text, "who averages more assists this season", None) == 2
        assert routing.ungrounded_numbers(text, "who averages more assists this season", None,
                                          season_line=self.SEASON_LINE) == 0
        assert routing.ungrounded_numbers(text.replace("2025-26", "2025\u201326"), "more assists this season", None,
                                          season_line=self.SEASON_LINE) == 0
        # A season only the StatMuse question names counts too
        assert routing.ungrounded_numbers("Asking StatMuse about 2015-16", "best record nine years ago", None,
                                          statmuse_query="best record 2015-16") == 0

    def test_naming_a_season_does_not_excuse_its_digits_elsewhere(self):
        text = "He averaged 26 points in 2025-26"
        assert routing.ungrounded_numbers(text, "how was he this season", None, season_line=self.SEASON_LINE,
                                          statmuse_query="Jayson Tatum points per game 2025-26") == 1
        # Nor do the season line's other numbers (its opening date) excuse anything
        assert routing.ungrounded_numbers("Up by 20 with 10 left", "am I winning", None,
                                          season_line=self.SEASON_LINE) == 2

    def test_other_numbers_in_the_statmuse_question_may_be_repeated(self):
        assert routing.ungrounded_numbers("Asking StatMuse about the 2024 Finals", "luka in the finals two years ago",
                                          None, statmuse_query="Luka Doncic stats 2024 Finals vs Celtics") == 0
        assert routing.ungrounded_numbers("He scored 41 in the 2024 Finals", "luka in the finals two years ago",
                                          None, statmuse_query="Luka Doncic stats 2024 Finals vs Celtics") == 1


@pytest.mark.unit
def test_describe_view_names_players_and_nba_teams_but_not_fantasy_teams(monkeypatch):
    from schemas.ai import AiContext
    seen = []

    async def fake(player_ids, nba_team):
        seen.append((player_ids, nba_team))
        return {SENGUN: "Alperen Sengun", SABONIS: "Domantas Sabonis"}, "Houston Rockets"
    monkeypatch.setattr(routing, "_view_names", fake)

    view, players = asyncio.run(routing.describe_view(AiContext(
        page="terminal", mode="player", player_id=SENGUN, compare_ids=[UNKNOWN, SABONIS], team_id=7, nba_team="HOU",
        window="l15")))

    assert seen == [([SENGUN, UNKNOWN, SABONIS], "HOU")]
    # Players by name only: the model names them back, so an ID here is only something to copy wrong
    assert view == {
        "page": "terminal", "mode": "player", "team_id": 7, "window": "l15",
        "player": "Alperen Sengun", "compare": ["Domantas Sabonis"],
        "nba_team": {"abbrev": "HOU", "name": "Houston Rockets"},
    }
    assert players == {SENGUN: "Alperen Sengun", SABONIS: "Domantas Sabonis"}
    assert str(SENGUN) not in json.dumps(view)


@pytest.mark.unit
def test_describe_view_lists_teams_by_what_the_server_knows_not_what_users_wrote():
    from schemas.ai import AiContext
    teams = [{"team_id": i, "team_name": f"Team {i}", "league_name": "L", "provider": "espn", "season": 2027,
              "scoring": "categories" if i % 2 else "points"} for i in range(1, 20)]

    view, _ = asyncio.run(routing.describe_view(AiContext(page="home", team_id=2), teams))

    assert len(view["teams"]) == routing.MAX_VIEW_TEAMS
    assert view["teams"][:2] == [
        {"team_id": 1, "scoring": "categories", "provider": "espn", "season": 2027},
        {"team_id": 2, "scoring": "points", "provider": "espn", "season": 2027, "selected": True},
    ]
    assert "team_name" not in json.dumps(view) and "league_name" not in json.dumps(view)
    assert "teams" not in asyncio.run(routing.describe_view(AiContext(page="home")))[0]


@pytest.mark.unit
def test_an_empty_view_needs_no_lookup(monkeypatch):
    from schemas.ai import AiContext

    async def fail(*args):
        raise AssertionError("no lookup for an empty view")
    monkeypatch.setattr(routing, "_view_names", fail)

    assert asyncio.run(routing.describe_view(AiContext(page="rankings"))) == ({"page": "rankings"}, {})


@pytest.mark.unit
class TestParse:
    def test_reads_the_final_json(self):
        answer = routing.parse_answer(json.dumps({
            "kind": "show", "text": " Opening Sengun ", "statmuse_query": None, "gap": None, "missing": None,
            "suggestions": [],
            "target": {"type": "terminal", "mode": "player", "player": " Alperen Sengun ",
                       "compare": ["Domantas Sabonis", "", "Domantas Sabonis", 7],
                       "team_id": None, "nba_team": None, "window": "l15"},
        }))
        assert isinstance(answer.target, TerminalTarget)
        assert (answer.text, answer.target.window) == ("Opening Sengun", "l15")
        # The names wait for `validate`; the IDs they become are not the model's to write
        assert (answer._player, answer._compare) == ("Alperen Sengun", ["Domantas Sabonis"])
        assert (answer.target.player_id, answer.target.compare_ids) == (None, [])

    def test_an_id_the_model_writes_anyway_is_dropped(self):
        raw = terminal_answer()
        raw["target"].update(player_id=3112335, compare_ids=[1, 2])
        answer = routing.parse_answer(json.dumps(raw))
        assert (answer.target.player_id, answer.target.compare_ids, answer._player) == (None, [], "Alperen Sengun")

    def test_a_name_is_capped(self):
        raw = terminal_answer()
        raw["target"]["player"] = "x" * 500
        assert len(routing.parse_answer(json.dumps(raw))._player) == routing.MAX_NAME

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
        assert answer.rejected_target["player"] == "Alperen Sengun"   # who it was for is kept with it

    @pytest.mark.parametrize("min_games, kept", [(0, None), (-1, None), (83, None), (100, None), (1, 1), (82, 82)])
    def test_a_games_minimum_out_of_range_is_no_minimum(self, min_games, kept):
        answer = routing.parse_answer(json.dumps(rankings_answer("rankings", min_games)))
        assert answer.kind == "show" and answer.target.rankings.min_games == kept

    @pytest.mark.parametrize("written", [None, "points", "categories"])
    def test_a_category_filter_means_the_categories_format(self, written):
        """/rankings drops `cats` in any other format: "top shot blockers" opened plain points rankings."""
        raw = rankings_answer("rankings", 20)
        raw["target"]["rankings"]["format"] = written
        assert routing.parse_answer(json.dumps(raw)).target.rankings.format == "categories"

    def test_no_category_filter_leaves_the_format_alone(self):
        raw = rankings_answer("rankings", None)
        raw["target"]["rankings"].update(cats=[], format=None)
        assert routing.parse_answer(json.dumps(raw)).target.rankings.format is None

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
class TestFold:
    @pytest.mark.parametrize("a, b", [
        ("Nikola Jokić", "nikola jokic"), ("De'Aaron Fox", "DeAaron Fox"), ("P.J. Washington", "PJ Washington"),
        ("Jimmy Butler III", "Jimmy Butler"), ("Jaren Jackson Jr.", "jaren jackson jr"),
        ("Shai Gilgeous-Alexander", "Shai Gilgeous Alexander"), ("  LeBron   James ", "lebron james"),
        # No accent to strip, so NFKD leaves the letter whole: folded the way Postgres `unaccent` folds it
        ("Nikola Đurišić", "Nikola Durisic"), ("Jørgen Łukasz Groß", "jorgen lukasz gross"),
    ])
    def test_the_same_name_written_two_ways_is_one_name(self, a, b):
        assert routing._fold(a) == routing._fold(b)

    def test_different_players_stay_different(self):
        assert routing._fold("Jalen Williams") != routing._fold("Jaylin Williams")
        assert routing._fold("Zion Williamson") != routing._fold("Zion Williams")
        assert routing._fold("...") == ()


@pytest.mark.unit
class TestPick:
    ROSTER = [(1, "Jalen Williams"), (2, "Jaylin Williams"), (3, "Mark Williams"), (4, "Zion Williamson"),
              (5, "Stephen Curry"), (6, "Seth Curry"), (7, "Jimmy Butler III"), (8, "Tacko Fall"), (9, "Tacko Fall"),
              (10, "Alex Sarr"), (11, "Nic Claxton"), (12, "Monte Morris"), (13, "Luguentz Dort"),
              (14, "Moritz Wagner"), (15, "Tre Jones"), (16, "Trey Jones")]

    def pick(self, name):
        return routing._pick(routing._fold(name), self.ROSTER)

    def test_the_whole_name_is_one_player_though_its_surname_is_many(self):
        assert self.pick("Jalen Williams") == ([(1, "Jalen Williams")], True)
        assert self.pick("jimmy butler") == ([(7, "Jimmy Butler III")], True)

    def test_part_of_a_name_is_everyone_it_fits_and_no_one_it_only_resembles(self):
        hits, whole = self.pick("Williams")
        assert ([i for i, _ in hits], whole) == ([1, 2, 3], False)       # not Williamson

    def test_a_shortened_first_name_finds_the_player(self):
        assert self.pick("Steph Curry") == ([(5, "Stephen Curry")], False)
        assert self.pick("S Curry") == ([], False)                         # too short to mean anyone
        assert self.pick("Curry")[0] == [(5, "Stephen Curry"), (6, "Seth Curry")]

    def test_a_player_listed_under_a_shorter_first_name_is_found_by_the_longer_one(self):
        """NBA.com lists Alexandre Sarr as Alex; the prompt tells the model to write names in full."""
        assert self.pick("Alexandre Sarr") == ([(10, "Alex Sarr")], False)
        assert self.pick("Nicolas Claxton") == ([(11, "Nic Claxton")], False)

    def test_the_loosest_reading_needs_the_surname_whole_and_exactly_one_player(self):
        assert self.pick("Treyvon Jones") == ([], False)       # Tre or Trey: never a "which one?"
        assert self.pick("Alexandre Sarro") == ([], False)     # the surname is not his
        assert self.pick("Marcus Morris") == ([], False)       # an initial and a surname is not Monte Morris
        assert self.pick("Lu Dort") == ([], False)             # too short to mean anyone
        assert self.pick("Moe Wagner") == ([], False)          # a nickname, not the start of Moritz
        assert self.pick("Sarr")[0] == [(10, "Alex Sarr")]     # a surname alone is still everyone with it
        # The only one among some of the Sarrs: the rest were past the limit, so nobody is picked
        assert routing._pick(routing._fold("Alexandre Sarr"), self.ROSTER, complete=False) == ([], False)
        assert routing._pick(routing._fold("Alex Sarr"), self.ROSTER, complete=False)[0] == [(10, "Alex Sarr")]

    def test_one_name_on_two_rows_is_reported_as_whole(self):
        assert self.pick("Tacko Fall") == ([(8, "Tacko Fall"), (9, "Tacko Fall")], True)

    def test_no_one_fits(self):
        assert self.pick("Michael Jordan") == ([], False)


@pytest.mark.unit
class TestCheckTerminal:
    def test_a_named_player_becomes_his_id(self):
        checked = check(named(window="l15"))
        assert checked.kind == "show" and checked.target.player_id == SENGUN and checked.target.window == "l15"

    def test_a_player_nobody_has_heard_of_is_said_plainly(self):
        """Not `invalid_target`: a name that matches nothing is the asker's typo as often as the model's slip."""
        checked = check(named("Nobody Atall", window="l15"))
        assert (checked.kind, checked.gap, checked.target) == ("cannot", "no_data", None)
        assert checked.text == "I couldn't find a player called Nobody Atall."
        assert checked.missing == "player not found: Nobody Atall"
        assert (checked.rejected_target["player"], checked.rejected_target["window"]) == ("Nobody Atall", "l15")
        assert check(named("Somebody Unlisted")).gap == "no_data"   # a name no lookup returned at all

    def test_a_name_that_fits_several_players_asks_which(self):
        checked = check(named("Thompson", window="l10"))
        assert (checked.kind, checked.gap, checked.target) == ("cannot", "ambiguous", None)
        assert checked.text == "Which Thompson do you mean: Amen Thompson, Ausar Thompson or Klay Thompson?"
        assert checked.suggestions == ["Show me Amen Thompson", "Show me Ausar Thompson"]
        assert checked.missing == "ambiguous player: Thompson (3 matches)"

    def test_a_long_list_of_candidates_is_cut_to_the_best_four(self):
        many = {"Williams": [(i, f"Player{i} Williams") for i in range(1, 8)]}
        checked = check(named("Williams"), players=many)
        assert checked.text == ("Which Williams do you mean: Player1 Williams, Player2 Williams, Player3 Williams, "
                                "Player4 Williams or someone else?")

    def test_player_mode_without_a_player_is_a_bug(self):
        checked = check(named(None))
        assert (checked.gap, checked.missing) == ("invalid_target", "rejected: player")

    def test_a_refused_target_is_kept_for_the_log_with_the_reason(self):
        """The row said only `invalid_target`: not what the model proposed, nor why it failed."""
        checked = check(named(None, compare=["Domantas Sabonis"], window="l15"))
        assert checked.rejected_target["compare"] == ["Domantas Sabonis"] and checked.rejected_target["window"] == "l15"
        assert "rejected_target" not in checked.model_dump() and "_rejected_target" not in checked.model_dump()
        assert "_player" not in checked.model_dump() and "player" not in checked.model_dump()

        theirs = check(show(PageTarget(page="matchup", team_id=NOT_MY_TEAM)))
        assert (theirs.missing, theirs.rejected_target["team_id"]) == ("rejected: team_id", NOT_MY_TEAM)

    def test_an_answer_that_passes_carries_no_rejected_target(self):
        checked = check(named())
        assert (checked.missing, checked.rejected_target) == (None, None)

    def test_the_comparison_drops_duplicates_and_the_focused_player(self):
        checked = check(named(compare=["Domantas Sabonis", "Alperen Sengun", "Nikola Jokic"]))
        assert (checked.target.player_id, checked.target.compare_ids) == (SENGUN, [SABONIS, JOKIC])

    def test_an_unknown_or_ambiguous_player_in_the_comparison_stops_the_answer(self):
        assert check(named(compare=["Domantas Sabonis", "Nobody Atall"])).gap == "no_data"
        assert check(named(compare=["Thompson"])).gap == "ambiguous"

    def test_more_than_four_to_compare_is_invalid(self):
        players = {f"P{i}": [(i, f"P{i}")] for i in range(1, 7)}
        checked = check(named("P1", compare=["P2", "P3", "P4", "P5", "P6"]), players=players)
        assert (checked.gap, checked.missing) == ("invalid_target", "rejected: compare")
        assert check(named("P1", compare=["P2", "P3", "P4", "P5"]), players=players).target.compare_ids == [2, 3, 4, 5]

    def test_team_mode_needs_one_of_the_callers_teams(self):
        mine = check(show(TerminalTarget(mode="team", team_id=MY_TEAM)))
        theirs = check(show(TerminalTarget(mode="team", team_id=NOT_MY_TEAM)))
        assert mine.target.team_id == MY_TEAM
        assert (theirs.kind, theirs.gap) == ("cannot", "invalid_target")

    def test_nba_team_is_normalized_and_checked(self):
        assert check(show(TerminalTarget(mode="nba_team", nba_team="hou"))).target.nba_team == "HOU"
        assert check(show(TerminalTarget(mode="nba_team", nba_team="XYZ"))).gap == "invalid_target"

    def test_fields_other_modes_cannot_use_are_cleared(self):
        """The client sets all three focus fields from the target; stray ones would win."""
        stray = named("Alperen Sengun", compare=["Domantas Sabonis"], mode="team", team_id=MY_TEAM, nba_team="HOU")
        checked = check(stray).target
        assert (checked.player_id, checked.compare_ids, checked.nba_team) == (None, [], None)
        assert check(named("Alperen Sengun", mode="overview")).target.player_id is None
        assert check(named("Nobody Atall", mode="overview")).kind == "show"   # a stray name is not looked at


@pytest.mark.unit
class TestCheckPageAndOthers:
    def test_rankings_keep_their_params(self):
        target = PageTarget(page="rankings", rankings=RankingsParams(window=14, cats=["blk"], format="categories"))
        checked = check(show(target)).target
        assert checked.rankings.window == 14 and checked.rankings.cats == ["blk"]

    def test_rankings_params_on_another_page_are_dropped(self):
        target = PageTarget(page="streamers", team_id=MY_TEAM, rankings=RankingsParams(window=7))
        assert check(show(target)).target.rankings is None

    def test_a_page_for_someone_elses_team_is_invalid(self):
        assert check(show(PageTarget(page="matchup", team_id=NOT_MY_TEAM))).gap == "invalid_target"

    def test_show_needs_a_target(self):
        assert check(show(None)).gap == "invalid_target"

    def test_statmuse_needs_a_question_and_carries_no_target(self):
        good = check(RouterAnswer(kind="statmuse", text="t", statmuse_query=" Jokic career triple doubles ",
                                  target=player()))
        bad = check(RouterAnswer(kind="statmuse", text="t", statmuse_query="  "))
        assert (good.kind, good.target, good.statmuse_query) == ("statmuse", None, "Jokic career triple doubles")
        assert (bad.kind, bad.gap) == ("cannot", "invalid_target")

    def test_cannot_carries_no_destination(self):
        checked = check(RouterAnswer(kind="cannot", text="No news", target=player(),
                                     statmuse_query="x", gap="no_data"))
        assert (checked.target, checked.statmuse_query, checked.gap) == (None, None, "no_data")


@pytest.fixture
def lookups(monkeypatch):
    """Record what `validate` asks the database for, and answer from PLAYERS and FOUND."""
    seen = SimpleNamespace(names=[], ids=[])

    async def fake_find(names):
        seen.names.append(list(names))
        return {name: PLAYERS.get(name, []) for name in names}

    async def fake_lookup(player_ids, team_ids, nba_teams, user_id):
        seen.ids.append((player_ids, team_ids, nba_teams, user_id))
        return FOUND
    monkeypatch.setattr(routing, "_find_players", fake_find)
    monkeypatch.setattr(routing, "_lookup", fake_lookup)
    return seen


@pytest.mark.unit
class TestValidate:
    def test_names_are_found_and_ids_checked_only_when_there_are_some(self, lookups):
        routed = asyncio.run(routing.validate(named(compare=["Domantas Sabonis"]), user_id=42))
        asyncio.run(routing.validate(show(PageTarget(page="matchup", team_id=MY_TEAM)), user_id=42))
        asyncio.run(routing.validate(RouterAnswer(kind="cannot", text="x", target=player()), user_id=42))
        asyncio.run(routing.validate(show(PageTarget(page="playoffs")), user_id=42))

        assert (routed.target.player_id, routed.target.compare_ids) == (SENGUN, [SABONIS])
        assert lookups.names == [["Alperen Sengun", "Domantas Sabonis"]]   # one round trip for both
        # Teams are checked against the caller; a cannot's stray target and a bare page ask nothing
        assert lookups.ids == [([], [MY_TEAM], [], 42)]

    def test_a_player_on_screen_needs_no_lookup(self, lookups):
        """The view named him to the model, and the model names him back -- written its own way."""
        on_screen = {JOKIC: "Nikola Jokić", SENGUN: "Alperen Sengun"}

        routed = asyncio.run(routing.validate(named("nikola jokic", compare=["Domantas Sabonis"], window="l30"),
                                              user_id=42, view_players=on_screen))

        assert (routed.target.player_id, routed.target.compare_ids, routed.target.window) == (JOKIC, [SABONIS], "l30")
        assert lookups.names == [["Domantas Sabonis"]]

    def test_part_of_an_on_screen_name_is_still_looked_up(self, lookups):
        """"Thompson" with Amen on screen is not settled by the server: the model says who it means."""
        routed = asyncio.run(routing.validate(named("Thompson"), user_id=42, view_players={1641708: "Amen Thompson"}))
        assert (routed.gap, lookups.names) == ("ambiguous", [["Thompson"]])

    def test_names_outside_player_mode_are_not_looked_up(self, lookups):
        asyncio.run(routing.validate(named("Alperen Sengun", mode="nba_team", nba_team="HOU"), user_id=42))
        assert (lookups.names, lookups.ids) == ([], [([], [], ["HOU"], 42)])


@pytest.mark.unit
def test_validate_names_the_nba_team_it_found(monkeypatch):
    """No tool returns NBA team names, so the lookup is the only place "76ers" can come from."""
    async def fake_lookup(player_ids, team_ids, nba_teams, user_id):
        return _Found(frozenset(), frozenset(), frozenset({"PHI"}), frozenset({"Philadelphia 76ers"}))
    monkeypatch.setattr(routing, "_lookup", fake_lookup)
    names = {"Alperen Sengun"}

    answer = asyncio.run(routing.validate(show(TerminalTarget(mode="nba_team", nba_team="phi")), user_id=42, names=names))

    assert answer.target.nba_team == "PHI"
    assert names == {"Alperen Sengun", "Philadelphia 76ers"}

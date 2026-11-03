"""
Dime's playbook (services/ai/playbook.md, appended to AGENT_PROMPT) and the
header line that tells it which points leagues score ESPN's default points.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest

from services.ai import agent, prompts
from services.ai.tools import points_scoring
from services.scoring import DEFAULT_POINT_WEIGHTS

pytestmark = pytest.mark.unit


# ---- the playbook ----

def test_the_playbook_holds_no_digits():
    # The made-up-number check (agent.ungrounded_numbers) counts only tool results and
    # the user's words as sources: a threshold Dime repeated from here would be logged
    # as made up. Write thresholds in words ("a handful of games").
    text = prompts.PLAYBOOK_PATH.read_text(encoding="utf-8")
    digits = [line for line in text.splitlines() if re.search(r"\d", line)]
    assert not digits, digits


def test_the_agent_prompt_is_dimes_rules_then_the_playbook():
    assert prompts.AGENT_PROMPT.startswith("You are Dime, ")
    assert "CV AI" not in prompts.AGENT_PROMPT
    assert prompts.AGENT_PROMPT.endswith("\n\n" + prompts.PLAYBOOK)
    assert prompts.PLAYBOOK.startswith("Your playbook:")
    # Byte-stable: no trailing whitespace a re-save could change, nothing volatile
    assert prompts.PLAYBOOK == prompts.PLAYBOOK.strip()
    assert not re.search(r"\d{4}-\d{2}-\d{2}", prompts.AGENT_PROMPT)


def test_the_playbook_carries_the_checks_before_a_move():
    for check in ("Sample size", "Draft cost", "Projections", "Track record", "Role", "Health"):
        assert f"  - {check}:" in prompts.PLAYBOOK, check
    # The planners' own pick of who to give up gets the same checks
    assert "plan_streams and project_matchup choose the player to give up" in prompts.PLAYBOOK
    assert "run your playbook's checks" in prompts.AGENT_PROMPT


def test_the_rules_no_longer_ask_for_a_caveat_on_fpts():
    assert "not the user's league scoring" not in prompts.AGENT_PROMPT
    assert "Never add a caveat about which formula a number uses" in prompts.AGENT_PROMPT
    # ...but fpts is never passed off as a custom league's points
    assert "never present fpts as the points of a league with its own point values" in prompts.AGENT_PROMPT


# ---- which points leagues score ESPN's default ----

def _league(weights, synced=True, preview=None):
    return SimpleNamespace(point_weights=weights, settings_synced=synced, scoring_preview=preview)


def test_a_synced_points_league_on_the_default_weights_is_espn_default():
    assert points_scoring("points", _league(dict(DEFAULT_POINT_WEIGHTS))) == "espn_default"
    assert points_scoring("points", _league({})) == "espn_default"       # synced, no weights = the defaults


def test_a_points_league_with_its_own_values_is_custom():
    assert points_scoring("points", _league({**DEFAULT_POINT_WEIGHTS, "tov": -1.0})) == "custom"
    assert points_scoring("points", _league({**DEFAULT_POINT_WEIGHTS, "dd": 5.0})) == "custom"


def test_no_answer_for_categories_or_unsynced_leagues():
    assert points_scoring("categories", _league({})) is None
    assert points_scoring("points", _league(dict(DEFAULT_POINT_WEIGHTS), synced=False)) is None
    assert points_scoring("points", None) is None
    # A preview marks the summary synced whatever the league's own state: no label then
    assert points_scoring("points", _league({}, preview="points")) is None


def test_the_header_names_each_points_leagues_scoring(monkeypatch):
    async def teams(user_id):
        return [{"team_id": 7, "provider": "espn", "season": 2027, "scoring": "points",
                 "points_scoring": "espn_default"},
                {"team_id": 8, "provider": "espn", "season": 2027, "scoring": "points",
                 "points_scoring": "custom"},
                {"team_id": 9, "provider": "yahoo", "season": 2027, "scoring": "points", "points_scoring": None},
                {"team_id": 22, "provider": "espn", "season": 2027, "scoring": "categories",
                 "points_scoring": None}]

    async def season():
        return "NBA season: 2026-27 starts 2026-10-20."
    monkeypatch.setattr(agent, "list_my_teams", teams)
    monkeypatch.setattr(agent, "_season_line", season)

    line = asyncio.run(agent.header(42, 7))

    assert "team_id 7 (espn, points scoring: ESPN default points, 2027)" in line
    assert "team_id 8 (espn, points scoring: its own point values, 2027)" in line
    assert "team_id 9 (yahoo, points scoring, 2027)" in line
    assert "team_id 22 (espn, categories scoring, 2027)" in line

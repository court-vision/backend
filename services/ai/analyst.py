"""
The live rankings' analyst: one read of one player (the Live rankings plan,
step 4).

A box score cannot say "re-evaluated in two weeks", "starting while X is out"
or "on a minutes limit for his first games back". The stored news can. Each
morning the data platform screens for the players whose news might say such a
thing (a status change, a prominent teammate out or back, minutes far from the
projection, a new injury, lineup or transaction item, a note due to be read
again) and asks for one read of each (`POST /v1/internal/jobs/analyst/read`).

A read is one model call with a fixed prompt and a structured answer: write a
note, keep the one it has, retire it, or nothing. A note states inputs to the
projection (a return date with its range, minutes, a share of the offense, an
end, a day to read it again) and the stored items it rests on. It never states
a rank or a value: the engine that prices everyone prices the note.

What keeps it honest is on the data platform, which owns the tables: a note
that cites no stored item, or sits outside its bounds, is refused there, and in
shadow the analyst's notes reach only a second projection nobody reads. This
module asks, parses and counts the cost.

Spend is on a monthly line of its own (`services.ai.spend`, line `analyst`):
each read reserves `analyst_read_max_usd` before its call and is settled to
what the call cost, so a morning of reads cannot use up the AI desk's budget or
the check-ins'. A read that went out and never answered keeps its reservation,
because a request the API accepted may be billed after we stop listening; one
whose connection never opened sent nothing and gets its reservation back, so an
outage does not use up the month. A read is one attempt, never retried here.
With the month's budget used up the answer is `budget` and nothing is asked.

The prompt is a cached prefix: nothing volatile in it (no dates, no names).
The day, the player and everything about him are the user turn. It shares the
playbook's "Reading a player" rules with Dime (`services/ai/playbook.md`), read
once at import.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import anthropic
import httpx2
from pydantic import ValidationError

from core.errors import AppError, ProviderTimeout
from core.logging import get_logger
from core.settings import settings
from schemas.analyst import AnalystProposal, AnalystReadData, AnalystReadReq, AnalystSpendData, AnalystUsage
from services.ai import spend
from services.ai.prompts import PLAYBOOK
from services.ai.service import FALLBACK_BETA, _create, _Run, _served_content

# A change to the prompt or to the answer's schema is a new version: it is stored
# with every read, so a change in what the analyst writes can be traced to it.
PROMPT_VERSION = "analyst-1"
MAX_TOKENS = 8_000  # a backstop the model never sees; the answer is a short object
# A read is one attempt. The client's retry would hide a first attempt that went
# out and never answered behind whatever the second came to, and the month would
# count one call where two may be billed. The data platform asks again on its next
# run instead.
ATTEMPTS_AFTER_THE_FIRST = 0
# The client's own timeout sits past the read's deadline, so the deadline is what
# ends a slow read. Connecting gets a short one of its own: a connection that
# never opens fails as one, which is a failure known to have sent nothing.
CLIENT_TIMEOUT_MARGIN_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 10.0
# Transport failures that end before a request is written: nothing reached the API
NEVER_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)

_SECTION = "Reading a player:"


def reading_a_player(playbook: str = PLAYBOOK) -> str:
    """The playbook's "Reading a player" rules, as Dime reads them."""
    lines = playbook.splitlines()
    try:
        start = lines.index(_SECTION)
    except ValueError:
        return ""
    rules: list[str] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        rules.append(line)
    return "\n".join(rules)


_DATE = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}
_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_TEXT = {"anyOf": [{"type": "string"}, {"type": "null"}]}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["write", "keep", "retire", "none"]},
        "reason": {"type": "string"},
        "kind": {"type": "string", "enum": ["injury_current", "injury_return", "role", "trade", "other"]},
        "return_date": _DATE,
        "return_earliest": _DATE,
        "return_latest": _DATE,
        "out_for_season": {"type": "boolean"},
        "minutes": _NUMBER,
        "usage": _NUMBER,
        "until_date": _DATE,
        "until_player_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "confidence": {"type": "string", "enum": ["confirmed", "likely", "possible"]},
        "review_date": _DATE,
        "cites": {"type": "array", "items": {"type": "string"}},
        "against_owner_note": _TEXT,
    },
    "required": [
        "decision", "reason", "kind", "return_date", "return_earliest", "return_latest", "out_for_season",
        "minutes", "usage", "until_date", "until_player_id", "confidence", "review_date", "cites",
        "against_owner_note",
    ],
    "additionalProperties": False,
}

ANALYST_PROMPT = f"""\
You are the analyst behind Court Vision's fantasy basketball rankings. Every morning a \
statistical model reprices every NBA player for the rest of the season in three parts: \
skill (what he produces per minute), role (his minutes and his share of the offense, \
which move with who is out on his team) and games (the chance he plays each game left). \
The model reads box scores, the schedule and an availability feed. It cannot read. Your \
job is to read the stored news about one player and write down, as inputs to that model, \
what a box score cannot say.

You are asked four things about the player:
- How long is he out? "Re-evaluated in two weeks" is not "back in two weeks": a \
re-evaluation is the earliest he could be cleared, and a return usually follows it by a \
week or more.
- Is a change in his role temporary, or his to keep? A fill-in while a starter is out, a \
trade, a coach naming a starter, a rookie earning the job.
- Is he limited when he returns? A minutes limit for his first games back.
- Is there news with no box score yet? A trade this morning, a suspension, a planned rest.

What you are given, as one JSON object:
- `as_of`: the morning of the read. Every date you write is on or after it.
- `reasons`: why he was picked for a read today.
- `projection`: what the model assumes about him this morning. `out_now`, `return_date` \
and `out_for_season` are how it reads his availability: a `return_date` is the first \
game it has him back for, and `out_now` with no date means it knows only that he is out. \
`missed_in_a_row` is how many of his team's last games he has missed, and `games_played` \
his games this season. `minutes_usual` is what he plays when his team is whole, \
`minutes_last_five` what he played over his last five games, and `minutes` what the model \
projects from here. `games_expected` is how many of his team's `team_games_left` it \
expects him to play. `timeline` is the rest of his season in runs of alike games: first \
day, last day, his team's games in the run, the chance he plays each, his minutes, and \
his share of the offense against his usual one (1.0 is his usual share).
- `recent_games`: his last games, newest first, with his minutes, points, rebounds and \
assists.
- `availability`: the availability feed's latest `verdict` on him and the game it is \
for, with the feed's `reason`, the verdict he had `before` it, and the feed's \
`expected_return` when it gives one. Absent when the feed has no recent report on him.
- `teammates`: his team's rotation, most minutes first, with each one's usual `minutes`, \
whether the model has him `out`, the feed's `status` for him and the `expected_return` \
the model holds for him.
- `items`: the stored news items and availability reports about him of the last two \
weeks, newest first, with the source and when it was published, Eastern time. Each has \
a `ref`. Only an item with `citable` true can back a note: injury, availability, lineup \
and transaction items. A rumor or a performance recap never backs a change.
- `own_note`: the note you left on an earlier morning, if there is one, in the fields \
you answer with. `written` is the day you wrote it, and `standing` says what is left of \
it this morning: `state` is `in_force`, `ended` (he played, its date passed, or the \
teammate it waited on came back) or `review_due` (its review date has passed and it is \
no longer applied), `games_part_applies` and `role_part_applies` say which part still \
holds, and `why` gives the reason a part ended.
- `owner_note`: a note the site's owner wrote by hand, if there is one, in the same shape.
- `bounds`: the limits your note is held to: `minutes_max`, `usage_min`, `usage_max`, \
`review_by` (the latest review date) and `season_end` (the last day of the regular \
season).

What you answer:
- `decision`:
  - `write`: a note should stand from today. Give it whole: it replaces `own_note`.
  - `keep`: `own_note` still says it. Give only a new `review_date` and `cites`. The \
parts of it that still hold are carried on; a part that has ended is not brought back \
by keeping it. To change any of it, `write` it whole.
  - `retire`: `own_note` no longer holds and nothing replaces it. He played, the news \
was reversed, or the model has caught up. A note whose `standing` is `ended` is retired \
unless the items support a new one.
  - `none`: no note is warranted and there is none to retire.
- A note has two parts, and may have either or both:
  - Games: `return_date`, the single most likely first game back, with \
`return_earliest` and `return_latest` around it when the news gives a range or a \
re-evaluation date. Or `out_for_season` true, with no dates. Leave these null when the \
news says nothing the model does not already assume.
  - Role: `minutes` a game, `usage` (his share of the offense against his usual one), or \
both, as targets while the note holds. With them give the end: `until_date`, or \
`until_player_id`, a teammate from `teammates` who is out and whose return ends it. Give \
both when both apply. A role you judge to be his to keep has neither, and ends at the \
review date like every note.
- `kind`: `injury_current` for a player who is out, `injury_return` for limits on his way \
back, `role` for a change in minutes or usage, `trade`, or `other`.
- `confidence`: `confirmed` when the team or the league has said it, `likely` when a \
credible report has and nothing contradicts it, `possible` otherwise.
- `review_date`: the day the note should be read again, no later than `bounds.review_by`. \
Sooner when a re-evaluation or a decision is due before then. Past it the note is no \
longer applied, so every note expires.
- `cites`: the `ref` of every item the note rests on. A note with no citable item is \
refused.
- `reason`: one or two plain sentences for the owner: what the items say, and what \
follows for the projection. Name the source and the day of the item that matters most.
- `against_owner_note`: when `owner_note` exists and newer items disagree with it, one \
sentence saying how. Otherwise null. You never change the owner's note, and where both \
exist his is the one applied.

Rules:
- Write inputs, never a rank, a value or a projection of his stats. The model prices \
your inputs the way it prices everyone.
- Write only what the items say. When the items are silent, vague or contradicted, the \
answer is `none` (or `keep`, or `retire`). A note that repeats what `projection` already \
assumes adds nothing: the answer is `none`.
- A status with no timetable is not a return date. Do not turn "out" into a date the \
items never gave.
- A return date differs from the model's only when the items support a different one. \
Small differences are noise: write a date when it moves his return by about a week or \
more, when the model has no date at all, or when it wrongly has him back.
- A return date is on or after `as_of` and no later than `bounds.season_end`. A player \
the items have out past the end of the regular season is `out_for_season`.
- Stay inside `bounds`. `minutes` at most `bounds.minutes_max`: a backup cannot be given \
more minutes than the absent player left behind. `usage` between `bounds.usage_min` and \
`bounds.usage_max`.
- A fill-in's role ends when the player he fills in for returns. Tie it to that teammate \
with `until_player_id` rather than guessing a date.
- Fields that do not apply are null, false or empty.

How a good fantasy manager reads a player, which holds here too:
{reading_a_player()}
"""


def enabled() -> bool:
    return bool(settings.ai_enabled and settings.analyst_enabled)


def _turn(req: AnalystReadReq) -> str:
    """The user turn: everything about this player this morning. Names as written,
    not as \\u escapes."""
    return json.dumps(req.model_dump(mode="json", exclude_none=True), ensure_ascii=False)


def _usage(run: _Run) -> AnalystUsage:
    model = run.model or settings.analyst_model
    return AnalystUsage(
        model=model,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_input_tokens=run.cache_read_input_tokens,
        cache_creation_input_tokens=run.cache_creation_input_tokens,
        cost_micros=spend.cost_micros(
            model,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_input_tokens=run.cache_read_input_tokens,
            cache_creation_input_tokens=run.cache_creation_input_tokens,
        ),
        fallback=run.fallback,
    )


def parse(text: str) -> AnalystProposal:
    """The model's answer as a proposal. Raises ValueError when it is not one."""
    try:
        return AnalystProposal.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise ValueError(str(exc)) from exc


async def _ask(req: AnalystReadReq, run: _Run) -> Any:
    response = await _create(
        model=settings.analyst_model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": ANALYST_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _turn(req)}],
        thinking={"type": "adaptive"},
        output_config={"effort": settings.analyst_effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        max_retries=ATTEMPTS_AFTER_THE_FIRST,
        timeout=httpx2.Timeout(settings.analyst_read_timeout_seconds + CLIENT_TIMEOUT_MARGIN_SECONDS,
                               connect=CONNECT_TIMEOUT_SECONDS),
    )
    run.record(response)
    return response


def _went_unanswered(exc: BaseException) -> bool:
    """Whether a failed call may still be billed: the request may have gone out
    and no answer came back.

    Not billed, so False: an error the API answered with (a status), a call that
    failed before the client was reached, and a connection that never opened
    (`NEVER_SENT`: the name did not resolve, the connection was refused or timed
    out, no connection was free). Any other transport failure is one the request
    may have gone out on, and so is a call cut off from this side."""
    if isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError)):
        return True
    # The SDK's error is the cause of the app's, and the transport's the cause of
    # that. A timeout is a connection error to the SDK; a status error is not.
    sdk = exc.__cause__ if isinstance(exc, AppError) else None
    if not isinstance(sdk, anthropic.APIConnectionError):
        return False
    return not isinstance(sdk.__cause__, NEVER_SENT)


async def _month() -> tuple[int, int]:
    return await spend.analyst_month_spend()


def _data(outcome: str, month: tuple[int, int], **fields: Any) -> AnalystReadData:
    return AnalystReadData(outcome=outcome, prompt_version=PROMPT_VERSION,
                           month_spent_micros=month[0], month_cap_micros=month[1], **fields)


async def read(req: AnalystReadReq) -> AnalystReadData:
    """One read of one player. Never raises for a business outcome: off, out of
    budget, declined and unreadable are all answers. A provider failure raises,
    and the caller reads the player again on its next run."""
    if not enabled():
        return _data("off", (0, 0))
    reservation = await spend.reserve_analyst()
    if reservation is None:
        get_logger().warning("analyst_budget_reached", player_id=req.player_id)
        return _data("budget", await _month())

    run = _Run()
    started = time.monotonic()
    outcome, detail = "error", None
    proposal: Optional[AnalystProposal] = None
    unanswered = False
    try:
        try:
            response = await asyncio.wait_for(_ask(req, run), settings.analyst_read_timeout_seconds)
        except BaseException as exc:
            unanswered = _went_unanswered(exc)
            if isinstance(exc, asyncio.TimeoutError):
                raise ProviderTimeout("anthropic", "The analyst's read timed out", error_code="AI_TIMEOUT") from exc
            raise

        if response.stop_reason == "refusal":
            outcome = "declined"
        elif response.stop_reason == "max_tokens":
            outcome, detail = "malformed", "the answer was cut off"
        else:
            text = "".join(block.text for block in _served_content(response.content) if block.type == "text")
            try:
                proposal = parse(text)
                outcome = "read"
            except ValueError as exc:
                outcome, detail = "malformed", f"the answer could not be read: {str(exc)[:300]}"
    finally:
        if run.model_calls:
            await spend.settle(reservation, _usage(run).cost_micros)
        elif unanswered:
            # Sent and never answered: it reports no tokens and may still be billed,
            # so the month counts the read at its ceiling
            await spend.hold(reservation)
        else:
            await spend.cancel(reservation)
        get_logger().info(
            "analyst_read",
            player_id=req.player_id,
            as_of=req.as_of.isoformat(),
            outcome=outcome,
            decision=proposal.decision if proposal else None,
            model=run.model or settings.analyst_model,
            effort=settings.analyst_effort,
            prompt_version=PROMPT_VERSION,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_input_tokens=run.cache_read_input_tokens,
            cache_creation_input_tokens=run.cache_creation_input_tokens,
            fallback=run.fallback,
            items=len(req.items),
            duration_ms=round((time.monotonic() - started) * 1000),
            request_ids=run.request_ids,
            held=unanswered and not run.model_calls,
        )
    return _data(outcome, await _month(), proposal=proposal, detail=detail, usage=_usage(run))


async def month() -> AnalystSpendData:
    """What the reads have spent this month, and what they may."""
    spent, cap = await _month()
    return AnalystSpendData(
        enabled=enabled(),
        month_spent_micros=spent,
        month_cap_micros=cap,
        read_max_micros=round(settings.analyst_read_max_usd * 1_000_000),
        model=settings.analyst_model,
        prompt_version=PROMPT_VERSION,
    )

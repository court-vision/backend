"""
Schemas for the AI layer (docs/AI_LAYER_PLAN.md).

The answer is prose, but everything around it is structured on purpose: the
tool calls say which Court Vision lookups the answer rests on, and the usage
block makes each request's token spend visible to the caller as well as to
the `ai_request` log line.
"""

from datetime import date as date_type, datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field, model_validator

from schemas.app_map import BUILD_PATTERN
from schemas.common import ApiModel, BaseResponse
from schemas.lineup_editor import MoveRole

# Postgres text and jsonb cannot hold a NUL, so a string carrying one could
# never be logged; it is refused before any quota or model call is spent on it
NO_NUL = r"^[^\x00]*$"
# The same, and not blank: at least one character that is neither space nor NUL
# (pydantic's regex engine has no lookahead).
NO_NUL_NOT_BLANK = r"^[^\x00]*[^\s\x00][^\x00]*$"

# Every ID the caller sends is an int4 in our tables. Unbounded, a JSON integer
# can run to thousands of digits, each of them copied into the prompt.
INT4_MAX = 2_147_483_647


class AskReq(ApiModel):
    """Body for POST /v1/internal/ai/ask."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        pattern=NO_NUL,
        description="A question about NBA players, in plain language",
    )


class AiToolCall(ApiModel):
    """One Court Vision lookup the model made while answering."""

    name: str = Field(..., description="Tool name, e.g. get_player_stats")
    input: dict[str, Any] = Field(..., description="Arguments the model passed")
    is_error: bool = Field(..., description="True when the lookup failed or was refused")


class AiUsage(ApiModel):
    """Token spend for one request, summed over its model calls."""

    model: str = Field(..., description="Model that produced the final answer")
    model_calls: int = Field(..., description="Messages API calls made")
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    fallback: bool = Field(..., description="True when a refusal was re-served by a fallback model")


class AskData(ApiModel):
    answer: str = Field(..., description="The model's answer")
    tool_calls: list[AiToolCall] = Field(default_factory=list)
    usage: AiUsage


class AskResp(BaseResponse):
    """Response for POST /v1/internal/ai/ask."""

    data: Optional[AskData] = None


# ------------------------------------------------------------ Router (Phase 1)
#
# POST /v1/internal/ai/route takes a question to the place that answers it:
# a Court Vision view (`show`), StatMuse (`statmuse`), or an honest `cannot`
# (docs/AI_PHASE1_PLAN.md). The model picks the destination; the server
# validates it before anything reaches the client.

# `season`, or the last N games for N in 1..82 -- the terminal's `:window` range
WINDOW_PATTERN = r"^(season|l([1-9]|[1-7][0-9]|8[0-2]))$"
SEASON_GAMES = 82  # the same ceiling, for a games-played minimum

TerminalMode = Literal["overview", "player", "team", "nba_team"]
RoutablePage = Literal["rankings", "streamers", "matchup", "lineup-generation", "your-teams", "draft", "playoffs"]
AnswerKind = Literal["show", "statmuse", "cannot"]
# The last two are the server's alone: a destination it refused, and a player name that fits several players
GapKind = Literal["no_view", "no_data", "out_of_scope", "invalid_target", "ambiguous"]


class AiContext(ApiModel):
    """Where the user is when they ask. IDs only -- never names from league data,
    so the context adds no prompt-injection surface."""

    page: Optional[str] = Field(None, max_length=40, pattern=NO_NUL, description="Current route, e.g. 'terminal'")
    mode: Optional[TerminalMode] = Field(None, description="Terminal mode, when on the terminal")
    player_id: Optional[int] = Field(None, gt=0, le=INT4_MAX, description="Focused NBA player ID")
    compare_ids: list[Annotated[int, Field(gt=0, le=INT4_MAX)]] = Field(
        default_factory=list, max_length=4, description="NBA player IDs")
    team_id: Optional[int] = Field(None, gt=0, le=INT4_MAX, description="Selected fantasy team ID")
    nba_team: Optional[str] = Field(None, pattern=r"^[A-Z]{2,3}$", description="Focused NBA team abbreviation")
    window: Optional[str] = Field(None, pattern=WINDOW_PATTERN, description="Terminal stat window")


class RouteReq(ApiModel):
    """Body for POST /v1/internal/ai/route."""

    question: str = Field(..., min_length=1, max_length=500, pattern=NO_NUL,
                          description="The question, in plain language")
    context: AiContext = Field(default_factory=AiContext)


class TerminalTarget(ApiModel):
    """Open the terminal in a mode, focused on a subject."""

    type: Literal["terminal"] = "terminal"
    mode: TerminalMode
    player_id: Optional[int] = Field(None, description="NBA player ID; required for player mode")
    compare_ids: list[int] = Field(default_factory=list, description="NBA player IDs, at most 4; player mode only")
    team_id: Optional[int] = Field(None, description="The caller's fantasy team; required for team mode")
    nba_team: Optional[str] = Field(None, description="NBA team abbreviation; required for nba_team mode")
    window: Optional[str] = Field(None, pattern=WINDOW_PATTERN, description="`season` or `lN`")


class RankingsParams(ApiModel):
    """URL parameters for /rankings (frontend lib/rankings-params.ts)."""

    scope: Optional[Literal["global", "league"]] = None
    format: Optional[Literal["points", "categories"]] = None
    window: Optional[Literal[7, 14, 30]] = Field(None, description="Days; null means season")
    cats: list[str] = Field(default_factory=list, description="Category keys, e.g. ['blk', 'stl']")
    min_games: Optional[int] = Field(None, ge=1, le=SEASON_GAMES)


class PageTarget(ApiModel):
    """Navigate to a page, optionally selecting one of the caller's teams first."""

    type: Literal["page"] = "page"
    page: RoutablePage
    team_id: Optional[int] = Field(None, description="Set as the selected team before navigating")
    rankings: Optional[RankingsParams] = Field(None, description="Only when page is rankings")


AiTarget = Annotated[Union[TerminalTarget, PageTarget], Field(discriminator="type")]


class RouteData(ApiModel):
    kind: AnswerKind = Field(..., description="show: open `target` · statmuse: link out · cannot: say so")
    text: str = Field(..., description="One line for the user")
    target: Optional[AiTarget] = Field(None, description="Where to go, when kind is show")
    statmuse_query: Optional[str] = Field(None, description="The question restated in full, when kind is statmuse")
    statmuse_url: Optional[str] = Field(None, description="Built by the server from statmuse_query")
    suggestions: list[str] = Field(default_factory=list, description="Questions it can answer, when kind is cannot")
    gap: Optional[GapKind] = Field(None, description="Why a question could not be answered in Court Vision")
    question_id: Optional[int] = Field(None, description="For feedback; null if the question could not be logged")
    sources: list[AiToolCall] = Field(default_factory=list, description="Lookups the router made")
    usage: AiUsage


class RouteResp(BaseResponse):
    """Response for POST /v1/internal/ai/route."""

    data: Optional[RouteData] = None


class AiFeedbackReq(ApiModel):
    feedback: Optional[Literal["up", "down"]] = Field(..., description="null clears earlier feedback")


class AiFeedbackData(ApiModel):
    question_id: int
    feedback: Optional[Literal["up", "down"]] = None


class AiFeedbackResp(BaseResponse):
    """Response for POST /v1/internal/ai/questions/{question_id}/feedback."""

    data: Optional[AiFeedbackData] = None


# ---- The agent: the AI desk's conversations (docs/AI_AGENT_PLAN.md) ----

AGENT_MESSAGE_MAX = 2000


class AiConversationCreateReq(ApiModel):
    """Body for POST /v1/internal/ai/conversations."""

    team_id: Optional[int] = Field(None, gt=0, le=INT4_MAX,
                                   description="One of the caller's teams: what 'my team' means in this conversation")


class AiTurnReq(ApiModel):
    """Body for POST /v1/internal/ai/conversations/{conversation_id}/turns."""

    message: str = Field(..., min_length=1, max_length=AGENT_MESSAGE_MAX, pattern=NO_NUL)
    build: Optional[str] = Field(None, pattern=BUILD_PATTERN,
                                 description="The frontend build the user has open, its commit's full SHA: CV AI reads "
                                             "that build's app map, or the newest when it has none yet")


class AiTableColumn(ApiModel):
    key: str
    label: str


class AiTable(ApiModel):
    """A table the server built from the agent's tool results; the model never types its values."""

    type: Literal["table"] = "table"
    id: str = Field(..., description="Unique within its turn, e.g. 't1'")
    title: str
    result_ids: list[str] = Field(..., description="The tool results the rows were copied from")
    tools: list[str] = Field(..., description="The lookups behind those results")
    columns: list[AiTableColumn]
    rows: list[list[Any]] = Field(..., description="One list of cells per row, in column order")
    total_rows: int = Field(..., description="Rows before the table's limit")


class AiChartAxis(ApiModel):
    key: str
    label: str


class AiChartSeries(ApiModel):
    key: str
    label: str
    values: list[Optional[float]] = Field(..., description="One value per category, in order; null is a gap")


class AiChart(ApiModel):
    """A bar or line chart the server built from the agent's tool results; the model never types its values."""

    type: Literal["chart"] = "chart"
    id: str = Field(..., description="Unique within its turn, e.g. 'c1'")
    title: str
    kind: Literal["bar", "line"]
    result_ids: list[str] = Field(..., description="The tool results the values were copied from")
    tools: list[str] = Field(..., description="The lookups behind those results")
    x: AiChartAxis = Field(..., description="The column along the x axis")
    categories: list[str] = Field(..., description="The x values, in drawing order")
    series: list[AiChartSeries] = Field(..., min_length=1, max_length=4)
    total_rows: int = Field(..., description="X values before the chart's limit")


# shadow: made in one of Dime's check-ins (services/ai/checkins.py), checked but never sendable
AiProposalState = Literal["proposed", "sent", "dismissed", "expired", "shadow"]
# steer, lane, cancel_pickup: Dime's steering chips (services/ai/steering.py), applied through a
# Week desk route with `proposal_id`, never staged in the tray
AiProposalKind = Literal["transaction", "scheduled", "lineup", "steer", "lane", "cancel_pickup"]


class AiProposalPlayer(ApiModel):
    espn_id: int = Field(..., description="ESPN player id: what the roster transaction / scheduled pickup takes")
    nba_player_id: Optional[int] = Field(None, description="nba.players.id when known (Scout, headshot)")
    name: str
    team: Optional[str] = Field(None, description="NBA tricode")


class AiProposalMove(ApiModel):
    """One slot move of a lineup chip, as the lineup write takes it (`LineupMoveReq`: espn_id,
    from_slot_id, to_slot_id) and the tray shows it."""

    espn_id: int = Field(..., description="ESPN player id: `LineupMoveReq.player_id`")
    nba_player_id: Optional[int] = Field(None, description="nba.players.id when known (Scout, headshot)")
    name: str
    team: Optional[str] = Field(None, description="NBA tricode")
    from_slot_id: int = Field(..., description="The slot he is in on the chip's board")
    from_slot: str = Field(..., description="Its name (PG, UT, BE, IR, ...)")
    to_slot_id: int
    to_slot: str
    role: MoveRole = Field("shift", description="start (bench to active), bench (to the bench), shift (the rest)")
    note: Optional[str] = Field(None, description="The planner's note: his game, 'no game today', 'OUT'")


class AiHoldChange(ApiModel):
    """One hold a steer chip sets (usr.team_holds once applied)."""

    kind: Literal["keep", "block", "override", "pin"]
    player: AiProposalPlayer
    share: Optional[float] = Field(None, description="override only: the share of his usual value (0 = he misses)")
    starts_on: date_type
    ends_on: date_type
    effect: str = Field(..., description="What it does, in a line")


class AiHoldCleared(AiHoldChange):
    """A live hold a steer chip clears."""

    hold_id: int
    reason: str = Field(..., description="Why it was set")


class AiSteerAction(ApiModel):
    """A steer chip: applied with `POST /teams/{team_id}/holds` and its proposal_id."""

    holds: list[AiHoldChange] = []
    clear: list[AiHoldCleared] = []


class AiLaneDay(ApiModel):
    date: date_type
    value: float
    switch: bool = False
    player: AiProposalPlayer


class AiLaneAction(ApiModel):
    """A lane chip: `op` start (`POST /teams/{team_id}/stream-lanes` with `holder.espn_id` as player_id),
    pause or resume (`PATCH .../stream-lanes/{lane_id}`), stop (`DELETE .../stream-lanes/{lane_id}`),
    each with its proposal_id."""

    op: Literal["start", "pause", "resume", "stop"]
    lane_id: Optional[int] = Field(None, description="The lane; none for a start")
    holder: AiProposalPlayer = Field(..., description="Who holds the spot (a start: whose spot it streams)")
    status: Optional[str] = Field(None, description="pause, resume, stop: the lane's status when proposed")
    end_date: date_type
    gain: Optional[float] = Field(None, description="start: the projected gain over holding")
    adds: Optional[int] = None
    value: Optional[float] = None
    hold_value: Optional[float] = None
    adds_left: Optional[int] = None
    first_decision_at: Optional[datetime] = None
    days: list[AiLaneDay] = Field(default_factory=list, description="start: who would hold the spot each day")


class AiCancelPickupAction(ApiModel):
    """A cancel_pickup chip: applied with `DELETE /teams/{team_id}/pickups/{pickup_id}` and its proposal_id."""

    pickup_id: int
    date: date_type
    add: AiProposalPlayer
    drop: Optional[AiProposalPlayer] = None
    lane_id: Optional[int] = Field(None, description="The stream lane that scheduled it, if one did")


class AiProposalChip(ApiModel):
    """A move the agent proposed and the server checked: the chip the AI desk shows. Staged, it goes
    into the Week desk's tray (rendered in the AI desk) and is sent by its slider through the Week
    desk's own write with `proposal_id` on the request, which marks it sent: the roster transaction
    (`kind` transaction: `add` / `drop`, today), the scheduled pickup (`kind` scheduled: `add` / `drop`,
    a later day) or the lineup write (`kind` lineup: `moves`, one day's board, today's or a later
    day's, written at once). Never executed from here.

    Dime's steering chips (`kind` steer, lane, cancel_pickup) change no ESPN roster, so they are
    never staged: the AI desk applies one with a button through the Week desk's own route for it
    (`steer`, `lane` or `pickup` says which, and what it does), with `proposal_id`."""

    proposal_id: int
    state: AiProposalState = Field(..., description="proposed (can be staged), sent, dismissed, expired, or shadow "
                                                    "(a check-in's: what Dime would have done, never staged or sent)")
    kind: AiProposalKind
    team_id: int
    day: int = Field(..., description="Matchup day index, 0 = the matchup's first day")
    date: date_type = Field(..., description="The NBA date the move is for")
    dow: str = Field(..., description="Mon, Tue, ...")
    scoring_period_id: int = Field(..., description="ESPN's day for the move (the scheduled pickup's day; "
                                                    "a lineup chip's board, `ApplyLineupMovesReq.expected_scoring_period_id`)")
    add: Optional[AiProposalPlayer] = None
    drop: Optional[AiProposalPlayer] = None
    moves: list[AiProposalMove] = Field(default_factory=list, description="A lineup chip's slot moves; empty otherwise")
    summary: str = Field(..., description="One sentence: the move, its day, and what sending does")
    reason: str = Field(..., description="The agent's reason, as it wrote it")
    change: Optional[float] = Field(None, description="Projected change for the matchup (points, or category wins); "
                                                      "for a lineup chip the day's change in points, if a points league")
    scoring: Optional[str] = Field(None, description="The league's scoring format the change is in")
    expires_at: datetime = Field(..., description="When the chip stops being stageable (the day's first tip, or its rollover)")
    sent_at: Optional[datetime] = None
    # Dime's steering chips: what applying one does, under its kind (the rest above are empty)
    steer: Optional[AiSteerAction] = None
    lane: Optional[AiLaneAction] = None
    pickup: Optional[AiCancelPickupAction] = None


class AiProposal(AiProposalChip):
    """The chip as a block in a turn's parts."""

    type: Literal["proposal"] = "proposal"
    id: str = Field(..., description="Unique within its turn, e.g. 'p1'")


class AiPlace(ApiModel):
    """A place in the app, from the app map: the server builds its steps and link, the model never types them."""

    type: Literal["place"] = "place"
    id: str = Field(..., description="Unique within its turn, e.g. 'pl1' (a proposal's are p1, p2, ...)")
    screen_id: str = Field(..., description="The app map's id of the screen")
    desk: str = Field(..., description="The desk's name, e.g. 'Week'")
    steps: str = Field(..., description="The clicks from the desk's front, e.g. 'Week › Market P › ONE DAY'")
    href: str = Field(..., description="A path in the app: the screen's own link, or its desk's when it has none")
    direct: bool = Field(..., description="href opens the screen itself; otherwise the steps lead there from href")
    matches_build: bool = Field(..., description="The map is of the build the user has open")


AiDecisionArea = Literal["lineup", "streaming", "adds_drops", "other"]
AiDecisionAutomation = Literal["auto_lineup", "stream_lane", "scheduled_pickup", "none"]
AiDecisionCall = Literal["hold", "change"]
AiVerdict = Literal["agree", "disagree"]


AiActionOutcome = Literal["sent", "blocked", "refused", "failed"]


class AiDecisionAction(ApiModel):
    """What Dime did about a change on its own, on a team where that kind of move acts (the Dime
    autopilot plan, step 5; usr.autopilot_actions). A blocked or refused move is left as a chip."""

    outcome: AiActionOutcome = Field(..., description="sent (it went out) · blocked (a rule of the policy stopped it "
                                                      "before anything was sent) · refused (the route or ESPN said "
                                                      "no) · failed (ESPN unreachable, credentials, an error)")
    rule: Optional[str] = Field(None, description="The rule that blocked it (cap_day, cap_matchup, reserve, protected, "
                                                  "churn, paused, writes_off, ...) or the error code")
    message: str = Field(..., description="What happened, in a sentence")
    at: Optional[datetime] = None


class AiDecisionBlock(ApiModel):
    """One call a check-in made (usr.ai_decisions), holds included: shown in its conversation for the user
    to agree or disagree with. A change to the lineup or the roster names the shadow proposal behind it,
    the chip shown just above it in the same turn."""

    type: Literal["decision"] = "decision"
    id: str = Field(..., description="Unique within its turn, e.g. 'd1'")
    decision_id: int
    area: AiDecisionArea
    automation: AiDecisionAutomation = Field(..., description="Whose plan it reviewed; none for a move no "
                                                              "automation makes")
    call: AiDecisionCall = Field(..., description="hold: keep the plan (or do nothing) · change: do something else")
    summary: str = Field(..., description="What Dime would do, one line")
    reason: str
    proposal_id: Optional[int] = Field(None, description="The shadow proposal behind a change")
    verdict: Optional[AiVerdict] = Field(None, description="The user's call on it; null until they make one")
    action: Optional[AiDecisionAction] = Field(None, description="What Dime did about it on its own, where its kind "
                                                                 "of move acts; null in shadow and suggest")


AiBlock = Annotated[Union[AiTable, AiChart, AiProposal, AiPlace, AiDecisionBlock], Field(discriminator="type")]


class AiTextPart(ApiModel):
    kind: Literal["text"] = "text"
    text: str


class AiBlockPart(ApiModel):
    kind: Literal["block"] = "block"
    block: AiBlock


AiPart = Annotated[Union[AiTextPart, AiBlockPart], Field(discriminator="kind")]


class AiTurnView(ApiModel):
    id: int
    created_at: datetime
    message: str
    parts: list[AiPart] = Field(..., description="The answer as the user saw it: text and blocks, in order")
    outcome: str = Field(..., description="'ok', or the error code the turn ended with")
    feedback: Optional[Literal["up", "down"]] = None
    comment: Optional[str] = Field(None, description="The user's own note on this answer")
    cost_usd: float


# ---- Dime's check-ins (services/ai/checkins.py; the Dime autopilot plan, step 3) ----

AiCheckinKind = Literal["morning", "weekly", "pre_lock", "night"]
AiCheckinState = Literal["running", "done", "failed", "missed", "skipped"]


class AiCheckinSummary(ApiModel):
    """One check-in: a turn Dime started itself, about one fantasy day, in shadow."""

    checkin_id: int
    team_id: int
    kind: AiCheckinKind = Field(..., description="morning, weekly (a matchup's first morning), pre_lock (before "
                                                 "the first tip) or night (the next morning's follow-up)")
    date: date_type = Field(..., description="The fantasy day it is about")
    manual: bool = Field(..., description="Run from the AI desk rather than on the schedule")
    state: AiCheckinState
    reason: Optional[str] = Field(None, description="Why it failed, was missed or skipped (e.g. window_closed, "
                                                    "budget, daily_cap, AI_TIMEOUT)")
    due_at: datetime
    started_at: datetime
    finished_at: Optional[datetime] = None
    conversation_id: Optional[int] = Field(None, description="Its conversation; null when it never ran")
    title: str
    cost_usd: float
    decisions: int = Field(..., description="Calls it logged, holds included")
    changes: int = Field(..., description="Of those, the ones that would change an automation's plan or the roster")
    agree: int
    disagree: int
    sent: int = Field(0, description="Moves it sent on its own (on automations at AI)")


class AiCheckinSlot(ApiModel):
    kind: AiCheckinKind
    date: date_type
    due_at: datetime


AiLevel = Literal["off", "notify", "act", "ai"]
AiAutomationName = Literal["lineup", "daily_actions", "lanes", "late_scratch", "checkins"]


class AiLevelOption(ApiModel):
    """One level of an automation: whether it can be picked, and why not."""

    level: AiLevel
    available: bool
    reason: Optional[str] = Field(None, description="Why it can't be picked (not built yet, the account's "
                                                    "lineup email is off, ...)")


class AiAutomationRow(ApiModel):
    """One automation on the team (services/ai/autonomy.py): off, notify (an email), act (Court Vision does
    it, no AI) or ai (Dime does it)."""

    row: AiAutomationName
    label: str
    level: AiLevel = Field(..., description="Its level now (at AI while the autopilot switch is on)")
    options: list[AiLevelOption] = Field(..., description="Every level, in order off, notify, act, ai")
    note: Optional[str] = Field(None, description="When the row does less than its level says (paused, "
                                                  "acting off on the server, the lineup email off)")


class AiAutopilotTeam(ApiModel):
    """A team Dime may run check-ins on: its check-ins, the autopilot switch and its automations table."""

    team_id: int
    on: bool = Field(..., description="Check-ins run on the schedule (the Check-ins row at AI)")
    active_since: Optional[datetime] = Field(None, description="When it was last switched on or resumed")
    paused_at: Optional[datetime] = None
    paused_by: Optional[Literal["user", "system"]] = None
    paused_reason: Optional[str] = None
    next: Optional[AiCheckinSlot] = Field(None, description="The next scheduled check-in, while on")
    autopilot: bool = Field(False, description="The autopilot switch: every automation that offers AI is at AI")
    rows: list[AiAutomationRow] = Field(default_factory=list, description="The automations table, in order")


class AiActingPolicy(ApiModel):
    """The rules every move Dime sends itself is held to (core/settings.py)."""

    adds_per_day: int
    adds_per_matchup: int
    adds_reserve: int = Field(..., description="Adds this matchup Dime leaves for the user (injuries)")
    protected_top: int = Field(..., description="Dime never drops one of the roster's top this many by value")
    churn_hours: int = Field(..., description="Nor a player added this recently, unless he is out")
    pause_refused: int = Field(..., description="It pauses itself after this many refused writes in a day")
    pause_same_block: int = Field(..., description="...or this many blocks by the same rule in a day")


class AiActing(ApiModel):
    """Whether Dime can act on its own at all, and the policy it acts within."""

    available: bool = Field(..., description="DIME_ACTING_ENABLED: off, an automation at AI only reviews")
    steering: bool = Field(..., description="DIME_STEERING_ENABLED: Dime's steering chips exist at all")
    writes: bool = Field(..., description="ROSTER_WRITES_ENABLED: writes to ESPN are on")
    policy: AiActingPolicy


class AiFeatures(ApiModel):
    """The user's AI features, for the whole account (services/ai/ai_settings.py)."""

    memory: bool = Field(True, description="Dime keeps notes and reads them back")
    draft_rooms: bool = Field(True, description="Dime's side of the Draft desk")


class AiCheckinsData(ApiModel):
    teams: list[AiAutopilotTeam] = Field(..., description="The caller's teams Dime may run check-ins on")
    checkins: list[AiCheckinSummary] = Field(..., description="Newest first")
    month_usd: float = Field(..., description="This month's check-in spend, every autopilot team")
    cap_usd: float = Field(..., description="The check-ins' monthly budget, apart from the AI desk's")
    acting: Optional[AiActing] = None
    features: Optional[AiFeatures] = Field(None, description="The account's AI features")


class AiCheckinsResp(BaseResponse):
    """Response for GET /v1/internal/ai/checkins."""

    data: Optional[AiCheckinsData] = None


class AiCheckinRunReq(ApiModel):
    """Body for POST /v1/internal/ai/checkins: run one now."""

    team_id: int = Field(..., gt=0, le=INT4_MAX)
    kind: Optional[AiCheckinKind] = Field(None, description="Default: by the clock against today's first tip")


class AiCheckinResp(BaseResponse):
    data: Optional[AiCheckinSummary] = None


class AiAutopilotReq(ApiModel):
    """Body for PUT /v1/internal/ai/autopilot/{team_id}: the check-ins, the autopilot switch or the levels;
    what is left out stays as it is."""

    checkins: Optional[bool] = Field(None, description="true starts (or resumes, a pause Dime made itself "
                                                       "included) the team's check-ins, false pauses them")
    on: Optional[bool] = Field(None, description="The same as checkins (step 3's name for it)")
    autopilot: Optional[bool] = Field(None, description="The autopilot switch. On puts every automation that "
                                                        "offers AI at AI and turns check-ins and the AI "
                                                        "features on; off puts back what it changed")
    levels: Optional[dict[AiAutomationName, AiLevel]] = Field(
        None, description="Automations to set, to a level each offers. Changing one while autopilot is on "
                          "turns autopilot off and keeps the rest at AI")

    @model_validator(mode="after")
    def _something(self) -> "AiAutopilotReq":
        if self.on is not None and self.checkins is not None and self.on != self.checkins:
            raise ValueError("on and checkins disagree")
        if self.on is None and self.checkins is None and self.autopilot is None and not self.levels:
            raise ValueError("send checkins, autopilot or levels")
        return self


class AiAutopilotResp(BaseResponse):
    data: Optional[AiAutopilotTeam] = None


class AiSettingsReq(ApiModel):
    """Body for PUT /v1/internal/ai/settings: the features to change; what is left out stays as it is."""

    memory: Optional[bool] = None
    draft_rooms: Optional[bool] = None

    @model_validator(mode="after")
    def _something(self) -> "AiSettingsReq":
        if self.memory is None and self.draft_rooms is None:
            raise ValueError("send memory or draft_rooms")
        return self


class AiSettingsResp(BaseResponse):
    """Response for GET and PUT /v1/internal/ai/settings."""

    data: Optional[AiFeatures] = None


class AiDecisionVerdictReq(ApiModel):
    """Body for PUT /v1/internal/ai/decisions/{decision_id}/verdict."""

    verdict: Optional[AiVerdict] = Field(..., description="null clears it")


class AiDecisionVerdictData(ApiModel):
    decision_id: int
    verdict: Optional[AiVerdict] = None


class AiDecisionVerdictResp(BaseResponse):
    data: Optional[AiDecisionVerdictData] = None


class AiConversationSummary(ApiModel):
    id: int
    title: Optional[str] = Field(None, description="The first message, shortened; null until one is sent")
    team_id: Optional[int] = None
    kind: Literal["chat", "checkin"] = Field("chat", description="chat (the user's), or a check-in's conversation")
    created_at: datetime
    updated_at: datetime
    turns: int


class AiConversationDetail(AiConversationSummary):
    full: bool = Field(..., description="True when the conversation takes no more turns: start a new one")
    history: list[AiTurnView] = Field(..., description="Every turn, oldest first, failed ones included")
    checkin: Optional[AiCheckinSummary] = Field(None, description="The check-in that started it, for a check-in's "
                                                                 "conversation: its first turn's message is the "
                                                                 "server's brief, not the user's")


class AiConversationResp(BaseResponse):
    data: Optional[AiConversationDetail] = None


class AiConversationListResp(BaseResponse):
    data: Optional[list[AiConversationSummary]] = None


class AiUsageData(ApiModel):
    access: bool = Field(..., description="Whether the caller may use the agent (it is invite-only)")
    month_usd: float = Field(..., description="The caller's agent spend this calendar month (UTC)")
    cap_usd: float = Field(..., description="The caller's monthly cap")
    model: str
    effort: str


class AiUsageResp(BaseResponse):
    data: Optional[AiUsageData] = None


class AiTurnFeedbackData(ApiModel):
    turn_id: int
    feedback: Optional[Literal["up", "down"]] = None


class AiTurnCommentReq(ApiModel):
    """Body for POST /v1/internal/ai/turns/{turn_id}/comment."""

    comment: Optional[str] = Field(..., max_length=AGENT_MESSAGE_MAX, pattern=NO_NUL,
                                   description="The note; null or blank clears it")


class AiTurnCommentData(ApiModel):
    turn_id: int
    comment: Optional[str] = None


class AiTurnCommentResp(BaseResponse):
    data: Optional[AiTurnCommentData] = None


class AiProposalCheckData(ApiModel):
    chip: AiProposalChip
    problems: list[str] = Field(..., description="Why the chip can't be staged now; empty when it can")


class AiProposalResp(BaseResponse):
    """Response for GET /v1/internal/ai/proposals/{proposal_id}."""

    data: Optional[AiProposalCheckData] = None


class AiProposalDismissResp(BaseResponse):
    """Response for POST /v1/internal/ai/proposals/{proposal_id}/dismiss."""

    data: Optional[AiProposalChip] = None


class AiTurnFeedbackResp(BaseResponse):
    data: Optional[AiTurnFeedbackData] = None


# ---- Dime's notes (the AI desk's "What Dime knows"; services/ai/memory.py) ----

AiNoteKind = Literal["plan", "player", "league", "opponent", "preference", "lesson", "note"]


class AiNote(ApiModel):
    note_id: int
    kind: AiNoteKind
    team_id: Optional[int] = Field(None, description="The team it is about; null for a note about the user")
    text: str
    recheck_on: Optional[date_type] = None
    author: Literal["dime", "user"]
    written_on: date_type
    retired_on: Optional[date_type] = None
    retired_by: Optional[Literal["dime", "user"]] = None
    retired_reason: Optional[str] = None


class AiNoteListResp(BaseResponse):
    """Response for GET /v1/internal/ai/notes."""

    data: Optional[list[AiNote]] = None


class AiNoteResp(BaseResponse):
    data: Optional[AiNote] = None


class AiNoteCreateReq(ApiModel):
    """Body for POST /v1/internal/ai/notes: a note the user writes for the agent."""

    kind: AiNoteKind = "preference"
    text: str = Field(..., min_length=1, max_length=500, pattern=NO_NUL_NOT_BLANK)
    team_id: Optional[int] = Field(None, gt=0, le=INT4_MAX)
    recheck_on: Optional[date_type] = None


class AiNoteEditReq(ApiModel):
    """Body for PATCH /v1/internal/ai/notes/{note_id}: the note is replaced by the user's version."""

    text: Optional[str] = Field(None, min_length=1, max_length=500, pattern=NO_NUL_NOT_BLANK)
    kind: Optional[AiNoteKind] = None
    recheck_on: Optional[date_type] = None
    clear_recheck: bool = False

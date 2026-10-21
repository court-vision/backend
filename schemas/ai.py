"""
Schemas for the AI layer (docs/AI_LAYER_PLAN.md).

The answer is prose, but everything around it is structured on purpose: the
tool calls say which Court Vision lookups the answer rests on, and the usage
block makes each request's token spend visible to the caller as well as to
the `ai_request` log line.
"""

from datetime import date as date_type, datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

# Postgres text and jsonb cannot hold a NUL, so a string carrying one could
# never be logged; it is refused before any quota or model call is spent on it
NO_NUL = r"^[^\x00]*$"

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


AiProposalState = Literal["proposed", "sent", "dismissed", "expired"]
AiProposalKind = Literal["transaction", "scheduled"]


class AiProposalPlayer(ApiModel):
    espn_id: int = Field(..., description="ESPN player id: what the roster transaction / scheduled pickup takes")
    nba_player_id: Optional[int] = Field(None, description="nba.players.id when known (Scout, headshot)")
    name: str
    team: Optional[str] = Field(None, description="NBA tricode")


class AiProposalChip(ApiModel):
    """A roster move the agent proposed and the server checked: the chip the AI desk shows. Staged,
    it goes into the Week desk's tray (rendered in the AI desk) and is sent by its slider through
    the roster transaction (`kind` transaction, today) or the scheduled pickup (`kind` scheduled, a
    later day) with `proposal_id` on the request; the route marks it sent. Never executed from here."""

    proposal_id: int
    state: AiProposalState = Field(..., description="proposed (can be staged), sent, dismissed, or expired")
    kind: AiProposalKind
    team_id: int
    day: int = Field(..., description="Matchup day index, 0 = the matchup's first day")
    date: date_type = Field(..., description="The NBA date the move is for")
    dow: str = Field(..., description="Mon, Tue, ...")
    scoring_period_id: int = Field(..., description="ESPN's day for the move (the scheduled pickup's day)")
    add: Optional[AiProposalPlayer] = None
    drop: Optional[AiProposalPlayer] = None
    summary: str = Field(..., description="One sentence: the move, its day, and what sending does")
    reason: str = Field(..., description="The agent's reason, as it wrote it")
    change: Optional[float] = Field(None, description="Projected change for the matchup (points, or category wins)")
    scoring: Optional[str] = Field(None, description="The league's scoring format the change is in")
    expires_at: datetime = Field(..., description="When the chip stops being stageable (the day's first tip, or its rollover)")
    sent_at: Optional[datetime] = None


class AiProposal(AiProposalChip):
    """The chip as a block in a turn's parts."""

    type: Literal["proposal"] = "proposal"
    id: str = Field(..., description="Unique within its turn, e.g. 'p1'")


AiBlock = Annotated[Union[AiTable, AiChart, AiProposal], Field(discriminator="type")]


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


class AiConversationSummary(ApiModel):
    id: int
    title: Optional[str] = Field(None, description="The first message, shortened; null until one is sent")
    team_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    turns: int


class AiConversationDetail(AiConversationSummary):
    full: bool = Field(..., description="True when the conversation takes no more turns: start a new one")
    history: list[AiTurnView] = Field(..., description="Every turn, oldest first, failed ones included")


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

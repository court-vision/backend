"""
Schemas for the AI layer (docs/AI_LAYER_PLAN.md).

The answer is prose, but everything around it is structured on purpose: the
tool calls say which Court Vision lookups the answer rests on, and the usage
block makes each request's token spend visible to the caller as well as to
the `ai_request` log line.
"""

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

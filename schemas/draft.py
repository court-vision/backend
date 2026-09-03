"""Draft Lab schemas: draft sessions and their picks, and the ranked, cap-aware
board (rows + recommendations) those picks are scored against."""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import Field, model_validator

from .common import ApiModel, BaseRequest, BaseResponse, CategoryDefResp

DraftKind = Literal["live", "manual", "mock", "import"]
DraftStatus = Literal["active", "completed", "abandoned"]
DraftType = Literal["snake", "auction"]
PickSource = Literal["manual", "espn_sync", "import", "keeper"]


# ------------------------------- Sessions ------------------------------- #
#                          ------- Incoming -------                          #


class DraftKeeper(ApiModel):
    """One pre-designated keeper. Identity is whatever the client knows; the
    room resolves it the same way a pick is resolved."""

    player_id: Optional[int] = Field(default=None, description="NBA player id (nba.players.id)")
    espn_player_id: Optional[int] = None
    name: Optional[str] = None
    round: Optional[int] = Field(default=None, ge=1, description="Round the keeper costs, when the league assigns one")
    overall_pick: Optional[int] = Field(
        default=None,
        description=(
            "Computed on read, ignored on input: the pick this keeper consumes — the caller's "
            "slot in `round`, once the session has a slot and a pick order."
        ),
    )


class DraftSessionCreate(BaseRequest):
    """Everything omitted here is prefilled from the team's synced league.

    `my_slot` is the exception: `pick_order` holds ESPN team ids we cannot map
    back to `usr.teams`, so the user confirms their own slot at create time.
    """

    team_id: Optional[int] = Field(default=None, ge=1, description="Owned team to draft for; omit for a mock draft")
    kind: DraftKind = "manual"
    draft_type: Optional[DraftType] = Field(default=None, description="Defaults to the league's draft type, else snake")
    pick_order: Optional[List[int]] = Field(default=None, description="Provider team ids in first-round order; defaults to the league's")
    my_slot: Optional[int] = Field(default=None, ge=1, description="1-based slot the caller drafts from")
    rounds: Optional[int] = Field(default=None, ge=1, le=40, description="Defaults to the league's draftable roster size")
    keepers: List[DraftKeeper] = []


class DraftSessionUpdate(BaseRequest):
    """Partial update — only the fields present are written."""

    status: Optional[DraftStatus] = None
    draft_type: Optional[DraftType] = None
    pick_order: Optional[List[int]] = None
    my_slot: Optional[int] = Field(default=None, ge=1)
    rounds: Optional[int] = Field(default=None, ge=1, le=40)
    keepers: Optional[List[DraftKeeper]] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "DraftSessionUpdate":
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self


class DraftPickCreate(BaseRequest):
    """One pick. The player is identified by whatever the client has; the
    service resolves NBA id -> ESPN id -> normalized name, in that order."""

    player_id: Optional[int] = Field(default=None, description="NBA player id (nba.players.id)")
    espn_player_id: Optional[int] = None
    player_name: Optional[str] = Field(default=None, max_length=255)
    overall_pick: Optional[int] = Field(default=None, ge=1, description="Defaults to the session's next unused pick")
    by_me: bool = Field(default=False, description="Drafted by the caller (counts against position caps and fills the roster zone)")
    source: PickSource = Field(
        default="manual",
        description=(
            "`keeper` records a pick spent before the draft started (at the pick its round costs): "
            "it leaves the board like any pick but never counts as the draft front"
        ),
    )
    bid: Optional[float] = Field(default=None, ge=0, description="Auction price (v2; ignored by snake drafts)")

    @model_validator(mode="after")
    def _identifies_a_player(self) -> "DraftPickCreate":
        if self.player_id is None and self.espn_player_id is None and not self.player_name:
            raise ValueError("a pick needs player_id, espn_player_id or player_name")
        return self


#                          ------- Outgoing -------                          #


class DraftPickResp(ApiModel):
    overall_pick: int
    round: Optional[int] = Field(default=None, description="1-based round; None for auction drafts")
    slot: Optional[int] = Field(default=None, description="1-based slot in pick_order that made the pick")
    player_id: Optional[int] = Field(default=None, description="NBA player id, when the pick resolved to one")
    espn_player_id: Optional[int] = None
    player_name: Optional[str] = None
    by_me: bool = False
    source: PickSource = "manual"
    bid: Optional[float] = None
    created_at: Optional[datetime] = None


class DraftSessionResp(ApiModel):
    id: int
    team_id: Optional[int] = None
    league_id: Optional[int] = None
    kind: DraftKind
    status: DraftStatus
    draft_type: DraftType
    pick_order: List[int] = []
    my_slot: Optional[int] = None
    rounds: Optional[int] = None
    keepers: List[DraftKeeper] = []
    league_size: Optional[int] = Field(default=None, description="Teams in the draft: len(pick_order) when known")
    keeper_count: Optional[int] = Field(default=None, description="Keepers the league allows, from its draft settings")
    total_picks: Optional[int] = Field(default=None, description="league_size x rounds, when both are known")
    pick_count: int = Field(default=0, description="Picks recorded so far")
    next_overall_pick: int = Field(
        default=1,
        description=(
            "The number a new pick takes by default: the lowest unused one, so an undo is "
            "re-fillable in place. Not necessarily where the draft front is — see my_next_pick."
        ),
    )
    my_next_pick: Optional[int] = Field(
        default=None,
        description=(
            "Next overall pick belonging to my_slot, counted from the draft front rather than "
            "from a hole an undo left (snake drafts with a slot confirmed)"
        ),
    )
    picks_until_my_turn: Optional[int] = Field(
        default=None,
        description="Picks between the draft front and my turn; 0 means I am on the clock",
    )
    picks: List[DraftPickResp] = Field(default=[], description="Populated on the detail route; empty in list responses")
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DraftSessionResponse(BaseResponse):
    """One draft session."""
    data: Optional[DraftSessionResp] = None


class DraftSessionListResponse(BaseResponse):
    """Every draft session the caller owns, newest first."""
    data: List[DraftSessionResp] = []


class DraftPickResponse(BaseResponse):
    """The pick that was just recorded."""
    data: Optional[DraftPickResp] = None


class DraftPickDeleteResponse(BaseResponse):
    """The overall pick number that was undone."""
    data: Optional[int] = None


# --------------------------------- Board --------------------------------- #


class DraftBoardRow(ApiModel):
    player_id: int = Field(description="NBA player id (nba.players.id) — the id terminal panels expect")
    espn_id: Optional[int] = None
    name: str
    team: Optional[str] = Field(
        default=None,
        description="NBA team abbreviation from last season's stats; None for rookies (and anyone without a baseline row)",
    )
    position: Optional[str] = Field(default=None, description="NBA-style position (G, F, C, F-C, ...)")
    primary_position: Optional[str] = Field(
        default=None,
        description=(
            "ESPN primary position (PG/SG/SF/PF/C) from the market snapshot — what hard position "
            "caps are counted against. None until the market snapshot carries the player."
        ),
    )
    positions: Optional[List[str]] = Field(
        default=None,
        description=(
            "ESPN lineup slots the player is eligible for, verbatim and authoritative "
            "(a pure centre is simply not F-eligible). None until the market snapshot carries them."
        ),
    )
    injury_status: Optional[str] = Field(
        default=None, description="ESPN injury status (OUT, DAY_TO_DAY, ...); None when active or unknown"
    )
    cv_rank: Optional[int] = Field(
        default=None,
        description=(
            "Rank by CV value over the full pool, picked players included — a pre-draft big "
            "board rank that stays stable (and comparable to market_rank) as picks remove rows. "
            "None for market-only rows, which carry no value to rank."
        ),
    )
    value: Optional[float] = Field(
        default=None,
        description=(
            "League-scored per-game value: fantasy points under the league's weights, or the "
            "fpts-scale category value. None for a market-only row — a player ESPN ranks but "
            "neither a projection nor last season's baseline can value (a rookie, before projections)."
        ),
    )
    value_source: Literal["projection", "baseline", "market"] = Field(
        description=(
            "projection: ESPN's published per-game projection. baseline: last season's per-game "
            "averages. market: no stat line at all — the row exists because ESPN drafts him."
        ),
    )
    last_season_gp: Optional[int] = Field(default=None, description="Games played last season; None for rookies")
    projected_gp: Optional[int] = Field(default=None, description="Projected games this season, when a projection exists")
    fpts_avg: Optional[float] = Field(
        default=None,
        description="Per-game fantasy points under the platform default formula (familiar scale, tiebreak); None for market-only rows",
    )
    market_rank: Optional[int] = Field(default=None, description="ESPN editorial overall draft rank (latest snapshot)")
    adp: Optional[float] = Field(default=None, description="Average draft position across real ESPN drafts")
    auction_value: Optional[float] = Field(default=None, description="ESPN editorial auction value")
    market_delta: Optional[int] = Field(
        default=None,
        description="market_rank − cv_rank; positive means the market ranks the player worse than CV does (a bargain)",
    )
    cap_blocked: bool = Field(
        default=False,
        description=(
            "Drafting this player would exceed a hard per-position roster cap "
            "(league position_limits vs the caller's current roster). Shown greyed with a CAP badge, never hidden."
        ),
    )

    # Populated for category leagues only (None for points leagues).
    categories: Optional[dict[str, Optional[float]]] = Field(
        default=None,
        description="Per-game value per category; rates are 0-1 fractions (None when the player has no attempts)",
    )
    category_z: Optional[dict[str, float]] = Field(
        default=None,
        description="Signed z-score per category over the full pool (positive is always good; TOV is inverted)",
    )
    score: Optional[float] = Field(default=None, description="Sum of category z-scores; what `value` is mapped from")


class DraftRosterEntry(ApiModel):
    """A player the caller has drafted, with what the roster zone needs to place
    him: primary position for caps, eligible slots for the lineup, NBA team for
    stacking. The session's picks say when he was taken."""

    player_id: int
    name: str
    team: Optional[str] = None
    primary_position: Optional[str] = None
    positions: Optional[List[str]] = None
    value: Optional[float] = None
    value_source: Literal["projection", "baseline", "market"] = "baseline"
    injury_status: Optional[str] = None


class DraftBoardMeta(ApiModel):
    season: str                                 # season the board is for, e.g. "2026-27"
    format: str                                 # points | categories
    value_kind: Literal["fpts", "cat_value"]    # what the `value` column measures
    pool_size: int                              # full pool (picked included) — the cv_rank denominator
    available: int                              # rows returned: pool + market-only, minus everyone drafted
    projection_count: int                       # pool rows valued from a projection
    baseline_count: int                         # pool rows valued from last season's baseline
    market_only_count: int = 0                  # rows ESPN ranks that no stat line can value (rookies)
    projections_as_of: Optional[date] = None    # snapshot date of the projections used (None before ESPN publishes)
    market_as_of: Optional[date] = None         # snapshot date of the market ranks used
    session_id: Optional[int] = None            # set when the board was read for a draft session
    league_size: Optional[int] = None           # teams in the draft; sets replacement level with roster_slots
    roster_slots: dict[str, int] = {}           # the league's starting slots, as stored
    position_source: Literal["espn", "coarse", "none"] = Field(
        default="none",
        description=(
            "Where position data came from: espn (default_position_id on the market snapshot, "
            "exact caps), coarse (nba.players G/F/C, caps enforced per group only), none"
        ),
    )
    position_limits: dict[str, int] = {}        # the league's hard per-position caps, as stored ({"C": 4})
    categories: list[CategoryDefResp] = []      # empty for points leagues
    settings_synced: Optional[bool] = None      # whether the league's settings were read from the provider
    unsupported: list[str] = Field(
        default=[],
        description=(
            "League scoring keys the board cannot honor: dd/td are per-game bonuses, and the "
            "aggregate projection and season-baseline lines the board is valued from cannot carry them"
        ),
    )


class RecommendationComponent(ApiModel):
    """One visible term of a recommendation score, in season-value points."""

    key: Literal["season_value", "vorp", "scarcity", "flexibility", "injury"]
    label: str
    value: float
    in_score: bool = Field(
        description=(
            "Whether this term is summed into `score`. `season_value` is the base the other terms "
            "are computed from and is shown for context, not added on top of `vorp`."
        ),
    )
    detail: Optional[str] = Field(default=None, description="One line of why, for the room to render verbatim")


class DraftRecommendation(ApiModel):
    """One candidate for the caller's next pick, with the whole score decomposed."""

    player_id: int
    name: str
    primary_position: Optional[str] = None
    value: float = Field(description="Per-game league-scored value (the board row's `value`)")
    season_value: float = Field(description="value x projected games")
    vorp: float = Field(description="season_value minus the replacement level at the player's position")
    score: float = Field(description="vorp + scarcity + flexibility + injury — the ranking number")
    components: List[RecommendationComponent] = []
    reason: str = Field(description="One-sentence summary of the dominant terms")


class DraftBoardResp(BaseResponse):
    data: List[DraftBoardRow]
    recommendations: List[DraftRecommendation] = Field(
        default=[],
        description=(
            "Best available for the caller's next pick, best first. Cap-blocked players and rows "
            "with no value are never recommended. Empty when nothing can be valued."
        ),
    )
    roster: List[DraftRosterEntry] = Field(
        default=[],
        description="The caller's drafted players (session picks plus `mine`), in big-board order",
    )
    meta: Optional[DraftBoardMeta] = None

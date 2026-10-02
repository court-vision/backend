"""
Draft Lab board: every draftable player, valued by one league's scoring, plus
recommendations for the caller's next pick with every component visible.

Composes what already exists rather than inventing a new engine:

- Pool:   ESPN's published per-game projections (nba.player_projections, latest
          snapshot) where present, union a projection off each player's final
          previous-season row (services.scoring.projection) for everyone else,
          union market-only rows for players ESPN drafts that neither can value
          — a rookie is on the board from the day ESPN ranks him, with
          `value: null` and `value_source: market`, and upgrades in place when
          projections land.

          The baseline walks back: a player with no qualifying row in last
          season is valued off the most recent season he did play, and the row
          carries that season in `value_season` so the room can see a value is a
          year older than the rest of the board. Without it a player who missed
          a whole season had no value at all and sorted below the entire pool —
          going into 2026-27 that was four of ESPN's top 70.
- Value:  the same dispatcher math every provider uses — the league's point
          weights for points leagues, the fpts-scale category value
          (services.scoring.category_value) for category leagues.
- Market: ESPN editorial draft rank / auction value and crowd ADP from
          nba.draft_market (latest snapshot), joined by player id, with a
          `market_rank − cv_rank` delta. ESPN ranks the pool twice in the same
          payload — STANDARD for points leagues, ROTO for category leagues —
          and the board reads whichever matches the resolved format, so a
          category room is never handed the points board (they disagree by a
          mean of 28 places over ESPN's own top 150).
- Position: `default_position_id` (ESPN primary, 1-based) and `eligible_slot_ids`
          (0-based lineup slots) off the same snapshot. The two id spaces are
          kept apart: primary position drives caps, eligibility drives the
          lineup matching and which seats a player can fill.
- Caps:   hard per-position roster caps from usr.leagues.position_limits mark
          candidates the caller can no longer draft (flagged, never hidden, and
          never recommended).

- Fit:    category leagues also carry a second, roster-specific value
          (services.draft_fit): the same per-category z's re-weighted by how
          far this roster trails an average team, with punted categories at
          zero. `value` says what a player is worth to anyone, `fit_value`
          what he is worth here; both map through the same scale.
- Congestion: what a candidate would bench on the roster's real game nights
          (services.draft_congestion) — per-day lineup matching over every
          week the league scores, so five centres or four Nuggets cost what
          they cost.

cv_rank is computed over the FULL pool, picked players included, so it reads as
a pre-draft big-board rank: it stays stable as picks remove rows and remains
comparable to market_rank all draft long. `fit_rank` deliberately does not: it
ranks what is still available, and moves with every pick.

One `run_db` fetch materializes every input; one `run_cpu` call scores and
assembles the response (the rankings-service split — z-scoring a pool and
building hundreds of pydantic rows must not hold a DB permit).

Recommendations rank the available, non-cap-blocked pool by one of two orderings,
chosen by `rank_source`:

- `cv` (the default): Court Vision's room-aware pick, the room score

      score = value over replacement + punts + injury + congestion

  every term in one currency — season value under the league's own scoring — so
  the sum is interpretable, and each returned alongside the score.
  `season_value` (value × the games it is built on) is the base the rest are
  computed from and rides along as a non-summed component. Value over
  replacement is measured against the league's last starter still to be filled
  — one level for everybody, because most of a basketball lineup is seats
  anyone can fill, and a lower one only at a position whose own seats the
  league cannot fill (`_replacement_levels`); `punts` is what conceding
  categories does to it; `congestion` charges back the starts this roster
  could not use. Injury is priced once: a player whose games are projected has
  already paid for the ones he will miss, so the flat status discount applies
  only where nothing projects his games. What the roster is short of is
  information on the card, not a term — weighting categories by need lost to
  leaving them alone in the redraft experiments.

- `espn`: ESPN's own draft rank for the league's format, best rank first — the
  next name off the board the room is already ordered by, for a drafter who
  wants none of our opinion in the pick at all.

Both orderings compute every term either way, so an ESPN-ordered recommendation
still carries CV's full score as the visible dissenting opinion, and a CV-ordered
one names ESPN's rank so the disagreement is on the card. `rank_source` falls
back to `cv` when no market snapshot has been taken, and the meta says so.

The board's own row order is a separate question from the strip's. `board`
chooses it — `espn` by default, `cv` as the opt-in for a drafter who would
rather draft off Court Vision's rankings outright, `my_team` for those same
rankings re-ordered for the caller's roster — and `rank_basis`
(services.draft_market.rank_basis_for) says what actually ran: ESPN's
published rank in the gutter of every ESPN room — a league ESPN runs, a room
with no league at all, or one following an ESPN draft — with CV's rank beside
it; CV's order when asked for, for a room whose league lives elsewhere, or
while no snapshot exists. Rows come back already in that order, `board_rank`
naming each row's place. Players the basis does not rank trail every ranked one
in the other opinion's order with no number of their own, and a rookie ESPN
ranks that no stat line can value sits at his ESPN rank instead of below the
whole pool.

`my_team` is the room score above, applied to the whole board instead of to
five cards: every row carries `room_score` and `room_rank` whichever basis
orders it, and under `my_team` the rows come back in that order. It is the one
ordering that moves with every pick — a player the roster can no longer start
most nights slides, a position being picked clean climbs — and the one that
answers to the room's punts. Cap-blocked players have no place on it.

Availability answers "will he still be there when I pick again?" as a bucket —
likely / toss-up / gone — from the gap between ESPN's ADP and the caller's next
turn. ESPN publishes a point estimate, not a distribution, so a percentage
would imply a calibration that does not exist (plan diff #6).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, Iterable, Mapping, Optional, Sequence

from core.compute import run_cpu
from core.settings import settings
from db import base as db_base
from db.models.drafts import DraftPick
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from schemas.common import ApiStatus, CategoryDefResp
from schemas.draft import (
    CategoryNeedResp,
    DraftBoardMeta,
    DraftBoardResp,
    DraftBoardRow,
    DraftCongestionResp,
    DraftPlayoffsResp,
    DraftRecommendation,
    DraftRosterEntry,
    DraftStackResp,
    RecommendationComponent,
)
from services import schedule_service
from services.draft_congestion import (
    DEFAULT_SEASON_WEEKS,
    ESPN_POSITIONS,
    NON_STARTING_SLOTS as _NON_STARTING_SLOTS,
    CongestionModel,
    CongestionPlayer,
    Penalty,
    SampleWeek,
    active_slots,
    build_congestion_model,
    week_from_calendar,
)
from services.draft_fit import FitModel, build_fit_model, draftable_tier_size
from services.draft_market import market_auction_of, market_rank_of, rank_basis_for, rank_type_for
from services.draft_service import (
    draft_front,
    next_pick_for_slot,
    resolve_lagging_picks,
    rounds_from_roster_slots,
)
from services.player_value_service import PlayerValueService
from services.rankings_service import GAME_ONLY_KEYS
from services.scoring.category_rank import PoolRow
from services.scoring.category_value import (
    CATEGORY_VALUE_SCALE,
    category_value,
    rankable_categories,
)
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.pool import baseline_season, load_baseline_pool
from services.scoring.providers.espn_settings import POSITION_ID_MAP
from services.valuation.engine import (
    DEFAULT_GAMES,
    DEFAULT_PLAYOFF_WEIGHT,
    PLAYOFF_WEIGHTS,
    DEFAULT_LEAGUE_SIZE,
    DEFAULT_ROSTER_SIZE,
    LeagueModel,
    ProjectedPlayer,
    TeamWeeks,
    Valued,
    value_pool,
)
from services.valuation.playoffs import PlayoffSchedule, playoff_schedule, playoff_window
from utils.espn_helpers import POSITION_MAP

if TYPE_CHECKING:  # pragma: no cover
    from api.deps import OwnedDraftSessionContext
    from services.scoring.resolver import ResolvedScoring

# ESPN cap position -> the coarse nba_api position group it belongs to, and how
# many ESPN positions each group holds (see `_enforceable_caps`).
_COARSE_GROUP: dict[str, str] = {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}
_GROUP_SIZE: dict[str, int] = {"G": 2, "F": 2, "C": 1}

# The lineup vocabulary — ESPN_POSITIONS, the non-starting slots, and how a
# league's slots expand into seats — lives in services.draft_congestion, whose
# matching needs it too; it is imported above.

VALUE_DECIMALS = 1

# Games a player is assumed to play when nothing projects him. Roughly a healthy
# rotation season; it scales every candidate the same way, so it moves the size
# of the numbers, not the order.
DEFAULT_PROJECTED_GP = 65

RECOMMENDATION_COUNT = 5

# ESPN's default lineup: what an ESPN mock lobby drafts for, and so what a room
# with no league of its own is taken to field — the same assumption that gives
# it ESPN's default playoff weeks. Thirteen roster spots, ten of them starters.
DEFAULT_ROSTER_SLOTS: dict[str, int] = {
    "PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3, "BE": 3, "IR": 1,
}
# Starting seats assumed for a league whose own lineup never synced. Only
# places the replacement level: with no lineup, nothing can be benched.
DEFAULT_STARTERS = 10

# How far ADP has to sit from the pick in question before the answer stops
# being "it depends". Half a round of picks, floored so a tiny league still
# leaves room for a toss-up band.
AVAILABILITY_MIN_THRESHOLD = 3

# ESPN injuryStatus -> the share of season value a candidate is discounted by,
# when nothing projects his games (a projection that does has already priced
# the ones he will miss). ACTIVE (and anything unknown) is no discount at all.
INJURY_PENALTY: dict[str, float] = {
    "OUT": 0.15,
    "INJURY_RESERVE": 0.15,
    "SUSPENSION": 0.10,
    "DOUBTFUL": 0.10,
    "QUESTIONABLE": 0.05,
    "DAY_TO_DAY": 0.05,
}


@dataclass(frozen=True)
class BoardSession:
    """The draft-session facts the board needs, detached from the ORM.

    The last three are derived from the session's own picks and so are unknown
    until they have been read: `_with_geometry` fills them in between the fetch
    and the build. Everything that computes them lives in `draft_service` and
    is guarded by the replay harness — this never re-derives the arithmetic.
    """

    session_id: Optional[int] = None
    my_slot: Optional[int] = None
    rounds: Optional[int] = None
    league_size: Optional[int] = None
    draft_type: str = "snake"
    punts: tuple[str, ...] = ()
    rank_source: str = "cv"                 # cv | espn — what orders recommendations
    # espn | cv | my_team — whose rankings the caller asked to order the rows
    board_source: str = "espn"
    # How much a game in the league's fantasy-playoff weeks counts against one in
    # the regular season: the drafter's λ, 1 (off) to 4. ESPN's order never moves with it.
    playoff_weight: float = DEFAULT_PLAYOFF_WEIGHT
    espn_league_id: Optional[int] = None    # the ESPN draft the room follows, when it does
    draft_front: Optional[int] = None       # one past the last pick made on the clock
    my_next_pick: Optional[int] = None      # my next turn, counted from the front
    my_following_pick: Optional[int] = None  # and the turn after that

    @classmethod
    def of(
        cls, ctx: "OwnedDraftSessionContext", rank_source: str = "cv", board_source: str = "espn",
        playoff_weight: float = DEFAULT_PLAYOFF_WEIGHT,
    ) -> "BoardSession":
        return cls(
            session_id=ctx.session_id,
            my_slot=ctx.my_slot,
            rounds=ctx.rounds,
            league_size=ctx.league_size,
            draft_type=ctx.draft_type,
            punts=tuple(ctx.punts),
            rank_source=rank_source,
            board_source=board_source,
            playoff_weight=playoff_weight,
            espn_league_id=ctx.espn_league_id,
        )


@dataclass
class MarketOnlyRow:
    """A player ESPN drafts that no stat line can value — a rookie, in practice."""

    id: int
    name: str
    espn_id: Optional[int] = None
    position: Optional[str] = None


@dataclass
class BoardInputs:
    """Everything the board needs from the database, fully materialized."""

    season: str
    pool: list[PoolRow]                                 # one row per player: projection line, else baseline
    source: dict[int, str] = field(default_factory=dict)            # player id -> projection | baseline
    # Baseline rows only: the season the line was taken from. Usually last
    # season; older for a player walked back past a season he missed.
    value_season: dict[int, str] = field(default_factory=dict)
    last_season_gp: dict[int, int] = field(default_factory=dict)    # players with a baseline row
    projected_gp: dict[int, Optional[int]] = field(default_factory=dict)
    projections_as_of: Optional[date] = None
    projection_source: Optional[str] = None             # cv | espn — whose projection `source == "projection"` rows use
    # Per-game double- and triple-double rates, from the CV projection: what
    # lets a points league's dd/td weights score instead of reading 0.
    game_rates: dict[int, tuple[float, float]] = field(default_factory=dict)
    market: dict[int, dict] = field(default_factory=dict)           # player id -> market + position fields
    market_as_of: Optional[date] = None
    market_only: list[MarketOnlyRow] = field(default_factory=list)  # ranked, unvaluable players
    positions: dict[int, Optional[str]] = field(default_factory=dict)   # nba_api coarse position
    names: dict[int, tuple[str, Optional[int]]] = field(default_factory=dict)   # id -> (name, espn_id)
    session_picked: frozenset[int] = frozenset()        # drafted by anyone, from usr.draft_picks
    session_mine: frozenset[int] = frozenset()          # drafted by the caller
    used_picks: tuple[int, ...] = ()                    # pick numbers recorded in the session
    keeper_picks: tuple[int, ...] = ()                  # the subset spent before the draft started
    # Seat (1-based slot in pick_order) -> the players it has drafted. Every
    # seat, mine included; the fit model drops mine before pacing against them.
    seat_players: dict[int, frozenset[int]] = field(default_factory=dict)
    # Current NBA team per wanted player (nba.player_profiles): the board's first
    # choice ahead of last season's stats team, which goes stale every summer.
    current_team: dict[int, Optional[str]] = field(default_factory=dict)
    # The fantasy weeks the congestion term is measured on — the whole calendar,
    # so nothing is extrapolated from a sample — and how many weeks they stand
    # for; empty / 0 when the calendar could not be read.
    schedule_weeks: tuple[SampleWeek, ...] = ()
    season_weeks: int = 0
    # Every fantasy week of the season as per-day team sets: where the league's
    # playoff weeks fall and how each team's games split around them. Empty when
    # the calendar could not be read — the valuation then counts plain games.
    calendar: tuple[SampleWeek, ...] = ()


@dataclass
class _Terms:
    """One candidate's room score, term by term, with everything a detail line
    reads kept alongside, so building his card recomputes nothing."""

    c: dict
    position: Optional[str]
    season_value: float     # Court Vision's balanced value, every category counted
    room_value: float       # the same, with the room's punted categories left out
    bar: float              # the replacement level he is measured against, balanced
    room_bar: float         # and in the room's values
    bar_position: Optional[str]     # the short position `bar` came from; None for the league's last starter
    league_size: int
    vorp: float
    punts: float            # what the room's punts add to (or take from) his value over replacement
    injury: float
    category_fit: float     # information only: what the roster's needs would add
    congestion: float = 0.0
    congestion_detail: str = ""
    score: float = 0.0


class DraftBoardService:

    @staticmethod
    async def get_board(
        scoring: "ResolvedScoring",
        picked_ids: Iterable[int] = (),
        my_ids: Iterable[int] = (),
        session: Optional[BoardSession] = None,
    ) -> DraftBoardResp:
        """The board for one league: ranked rows minus everyone already drafted.

        `picked_ids` are NBA player ids drafted by anyone; `my_ids` the subset
        (not required to be repeated in `picked_ids`) drafted by the caller,
        which is what the position-cap check counts against. When `session` is
        given, its recorded picks are unioned with both — the room's picks live
        in `usr.draft_picks`, the stateless board passes them as query params,
        and the two answer the same way.
        """
        picked, mine = frozenset(picked_ids), frozenset(my_ids)
        board_session = session or BoardSession()
        inputs = await db_base.run_db(
            "draft_board.fetch", DraftBoardService._fetch_inputs, mine, board_session.session_id
        )
        board_session = DraftBoardService._with_geometry(board_session, inputs)
        return await run_cpu(
            "draft_board.build", DraftBoardService._build_board,
            scoring, picked, mine, inputs, board_session,
        )

    # ---- one trip to the database ---------------------------------------------

    @staticmethod
    def _fetch_inputs(my_ids: frozenset[int], session_id: Optional[int] = None) -> BoardInputs:
        season = settings.nba_season

        session_picked: set[int] = set()
        session_mine: set[int] = set()
        used_picks: list[int] = []
        keeper_picks: list[int] = []
        seat_players: dict[int, set[int]] = {}
        if session_id is not None:
            picks = list(
                DraftPick.select(
                    DraftPick.player, DraftPick.by_me, DraftPick.espn_player_id,
                    DraftPick.player_name, DraftPick.overall_pick, DraftPick.source,
                    DraftPick.slot,
                ).where(DraftPick.session == session_id)
            )
            # A pick recorded before its player reached nba.players carries
            # only the provider identity. Resolve it here, or the player would
            # be back on the board — and missing from the cap count — from the
            # day he synced, with nothing rewriting the row behind him.
            resolve_lagging_picks(picks)
            for pick in picks:
                used_picks.append(int(pick.overall_pick))
                if pick.source == "keeper":
                    keeper_picks.append(int(pick.overall_pick))
                if pick.player_id is None:
                    continue
                session_picked.add(pick.player_id)
                if pick.by_me:
                    session_mine.add(pick.player_id)
                # Whose roster this pick joined. Seats come from `pick_order`
                # (the ESPN team that picked, where ESPN said so) and are what
                # turns the other rooms' picks from "off the board" into
                # eleven rosters the fit model can pace against.
                if pick.slot is not None:
                    seat_players.setdefault(int(pick.slot), set()).add(pick.player_id)

        # walk_back: a player who missed last season is valued off the most
        # recent one he played rather than dropping off the board entirely.
        baseline = {row.id: row for row in load_baseline_pool(walk_back=True)}
        pool: dict[int, PoolRow] = dict(baseline)
        source = {pid: "baseline" for pid in baseline}
        last_season_gp = {pid: row.gp for pid, row in baseline.items()}
        # Only when it is NOT the season the board expects: the field's job is
        # to flag a value that is older than the rest, not to restate the
        # obvious for everyone who simply played last year.
        expected = baseline_season()
        value_season = {pid: row.season for pid, row in baseline.items()
                        if row.season and row.season != expected}

        # Court Vision's own projection when the cv-projection pipeline has
        # published one; ESPN's until then. Never a mix: one snapshot, one source.
        projection_source = "cv"
        projections_as_of, projections = DraftBoardService._latest_projections(season, "cv")
        if not projections:
            projection_source = "espn"
            projections_as_of, projections = DraftBoardService._latest_projections(season, "espn")
        projected_gp: dict[int, Optional[int]] = {}
        game_rates: dict[int, tuple[float, float]] = {}
        for rec in projections:
            line = StatLine.from_row(rec)
            gp = int(rec.projected_gp) if rec.projected_gp is not None else 0
            fpts = round(DEFAULT_POINTS.score(line), 1)
            base = baseline.get(rec.player_id)
            pool[rec.player_id] = PoolRow(
                id=rec.player_id, name=rec.player.name, team=(base.team if base else None),
                gp=gp, line=line, fpts_avg=fpts, fpts_total=round(fpts * gp, 1),
                espn_id=rec.player.espn_id, name_normalized=rec.player.name_normalized,
            )
            source[rec.player_id] = "projection"
            value_season.pop(rec.player_id, None)   # a projection is for the coming season
            projected_gp[rec.player_id] = int(rec.projected_gp) if rec.projected_gp is not None else None
            raw = getattr(rec, "raw", None) or {}
            if raw.get("dd_rate") is not None or raw.get("td_rate") is not None:
                game_rates[rec.player_id] = (float(raw.get("dd_rate") or 0.0), float(raw.get("td_rate") or 0.0))

        if projection_source == "cv" and projections:
            # Court Vision's projection covers every player on a roster who has
            # a stat line, so a baseline row it did not project belongs to a
            # player who is no longer on one — retired, overseas, unsigned.
            # Valuing him off last season's line put Russell Westbrook at #80
            # two months after he retired. He leaves the valued pool; if ESPN
            # still ranks him he stays on the board as a market-only row.
            projected = {rec.player_id for rec in projections}
            for pid in [pid for pid in pool if pid not in projected]:
                del pool[pid]
                source.pop(pid, None)
                value_season.pop(pid, None)
                last_season_gp.pop(pid, None)

        market: dict[int, dict] = {}
        market_as_of: Optional[date] = None
        for rec in DraftMarket.latest_for_season(season):
            market_as_of = rec.as_of_date
            market[rec.player_id] = {
                "overall_rank": int(rec.overall_rank) if rec.overall_rank is not None else None,
                "roto_rank": int(rec.roto_rank) if rec.roto_rank is not None else None,
                "adp": round(float(rec.adp), 2) if rec.adp is not None else None,
                "auction_value": float(rec.auction_value) if rec.auction_value is not None else None,
                "roto_auction_value": (float(rec.roto_auction_value)
                                       if rec.roto_auction_value is not None else None),
                "default_position_id": rec.default_position_id,
                "eligible_slot_ids": list(rec.eligible_slot_ids) if rec.eligible_slot_ids else None,
                "injury_status": rec.injury_status,
            }

        # Ranked players nothing can value yet (rookies, before projections):
        # on the board as market-only rows rather than invisible.
        market_only_ids = [pid for pid in market if pid not in pool]

        wanted = set(pool) | set(my_ids) | set(market_only_ids) | session_picked
        positions: dict[int, Optional[str]] = {}
        names: dict[int, tuple[str, Optional[int]]] = {}
        if wanted:
            for rec in Player.select(Player.id, Player.name, Player.position, Player.espn_id).where(
                Player.id.in_(list(wanted))
            ):
                positions[rec.id] = rec.position
                names[rec.id] = (rec.name, rec.espn_id)

        # Where each player plays now. The pool's team is last season's stats
        # row and goes stale with every offseason move; the profile is what the
        # congestion matching and the roster zone's stacks read. Raw FK values:
        # `rec.team` would fetch a team row per player.
        current_team: dict[int, Optional[str]] = {}
        if wanted:
            for rec in PlayerProfile.select(PlayerProfile.player, PlayerProfile.team).where(
                PlayerProfile.player.in_(list(wanted))
            ):
                current_team[rec.player_id] = rec.team_id
        calendar = DraftBoardService._calendar_weeks()

        market_only = [
            MarketOnlyRow(id=pid, name=names[pid][0], espn_id=names[pid][1], position=positions.get(pid))
            for pid in market_only_ids
            if pid in names
        ]

        return BoardInputs(
            season=season, pool=list(pool.values()), source=source,
            value_season=value_season,
            last_season_gp=last_season_gp, projected_gp=projected_gp,
            projections_as_of=projections_as_of,
            projection_source=projection_source if projections else None,
            game_rates=game_rates,
            market=market, market_as_of=market_as_of, market_only=market_only,
            positions=positions, names=names,
            session_picked=frozenset(session_picked), session_mine=frozenset(session_mine),
            used_picks=tuple(sorted(used_picks)), keeper_picks=tuple(sorted(keeper_picks)),
            seat_players={seat: frozenset(ids) for seat, ids in seat_players.items()},
            current_team=current_team,
            schedule_weeks=calendar, season_weeks=len(calendar),
            calendar=calendar,
        )

    @staticmethod
    def _calendar_weeks() -> tuple[SampleWeek, ...]:
        """The whole season's fantasy weeks as per-day team sets: what the
        playoff split and the congestion term both read.

        The calendar is a static file the schedule service caches once per
        process, so this is not a second trip anywhere. Empty when the season's
        calendar is not on disk: both then say so rather than pretending.
        """
        try:
            return tuple(
                week_from_calendar(w["matchup_number"], w["game_span"], w["games"])
                for w in schedule_service.iter_weeks()
            )
        except FileNotFoundError:
            return ()

    @staticmethod
    def _latest_projections(season: str, source: str = "espn") -> tuple[Optional[date], list]:
        """Every player's row from the latest projection snapshot, Player joined.

        (The mirrored model's `latest_for_season` returns bare rows; the board
        also needs each player's name/espn_id, so the join lives here.)
        """
        latest = (
            PlayerProjection.select(PlayerProjection.as_of_date)
            .where((PlayerProjection.season == season) & (PlayerProjection.source == source))
            .order_by(PlayerProjection.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if latest is None:
            return None, []
        records = (
            PlayerProjection.select(PlayerProjection, Player)
            .join(Player)
            .where((PlayerProjection.season == season)
                   & (PlayerProjection.source == source)
                   & (PlayerProjection.as_of_date == latest))
        )
        return latest, list(records)

    @staticmethod
    def _with_geometry(session: BoardSession, inputs: BoardInputs) -> BoardSession:
        """The session, told where the draft has got to.

        The front is one past the last pick made *on the clock* — not the
        lowest unused number, which an undo leaves behind — and my next two
        turns are counted from it. Availability is asked against those turns,
        so getting this wrong would grade every player against the wrong pick.
        Both rules live in `draft_service`, where the replay harness checks
        them against a real draft.
        """
        if session.session_id is None:
            return session

        front = draft_front(inputs.used_picks, inputs.keeper_picks)
        total_picks = (
            session.league_size * session.rounds
            if (session.league_size and session.rounds) else None
        )
        my_next = next_pick_for_slot(
            front, session.my_slot, session.league_size, session.draft_type,
            skip=inputs.keeper_picks, last=total_picks,
        )
        following = (
            next_pick_for_slot(
                my_next + 1, session.my_slot, session.league_size, session.draft_type,
                skip=inputs.keeper_picks, last=total_picks,
            )
            if my_next is not None else None
        )
        return replace(
            session, draft_front=front, my_next_pick=my_next, my_following_pick=following
        )

    # ---- pure assembly ---------------------------------------------------------

    @staticmethod
    def rank_pool(
        scoring: "ResolvedScoring",
        inputs: BoardInputs,
        cat_defs: list,
        session: Optional[BoardSession] = None,
    ) -> list[Valued]:
        """The pool in big-board order, best first — the order `cv_rank` enumerates.

        Valued the way this league scores (`services.valuation.engine`): its
        format, its categories or weights, its size, the games each player is
        expected to play, when his team plays them against the league's playoff
        weeks, and the drafter's playoff weight.

        Public because the mock autopicker and the recap need the same ladder.
        One ranking rule, three callers: an autopicker drafting by a second-hand
        approximation of CV value would make the mock's own board disagree with
        the room's, and a recap graded on another would grade a different draft.
        """
        model, _schedule = DraftBoardService.league_model(scoring, inputs, session or BoardSession(), cat_defs)
        players = [
            ProjectedPlayer(
                row=row,
                # 0 is "ESPN projects nobody", not "he will not play" — the
                # historical `gp or DEFAULT_PROJECTED_GP` rule.
                games=inputs.projected_gp.get(row.id) or None,
                team=inputs.current_team.get(row.id) or row.team,
                dd_rate=inputs.game_rates.get(row.id, (None, None))[0],
                td_rate=inputs.game_rates.get(row.id, (None, None))[1],
            )
            for row in inputs.pool
        ]
        return value_pool(players, model)

    @staticmethod
    def league_model(
        scoring: "ResolvedScoring",
        inputs: BoardInputs,
        session: BoardSession,
        cat_defs: list,
    ) -> tuple[LeagueModel, Optional[PlayoffSchedule]]:
        """The valuation's view of this league, and its playoff schedule.

        Roto is a category league with `win_mode == "roto"`: season totals, no
        weeks, no playoffs. A league-less room, or one whose settings never
        synced, gets ESPN's default playoff weeks — the window always exists, so
        the playoff column always has something true to say.
        """
        cats = scoring.categories
        roto = scoring.is_categories and cats is not None and cats.win_mode == "roto"
        fmt = "roto" if roto else ("categories" if scoring.is_categories else "points")
        league = scoring.league
        calendar = inputs.calendar
        window = playoff_window(
            getattr(league, "matchup_periods", None) if league is not None else None,
            getattr(league, "provider", None) if league is not None else None,
            len(calendar),
        )
        schedule = playoff_schedule(window, calendar) if (calendar and window.weeks) else None
        team_weeks, counted_weeks = DraftBoardService._team_weeks(calendar, window.weeks, roto)
        roster_size = session.rounds or rounds_from_roster_slots(DraftBoardService._roster_slots(scoring))
        model = LeagueModel(
            format=fmt,
            categories=tuple(cat_defs) if scoring.is_categories else (),
            point_weights=dict(scoring.points.weights),
            league_size=DraftBoardService._league_size(scoring, session) or DEFAULT_LEAGUE_SIZE,
            roster_size=roster_size or DEFAULT_ROSTER_SIZE,
            counted_weeks=counted_weeks,
            team_weeks=team_weeks,
            playoff_weight=session.playoff_weight,
        )
        return model, (None if roto else schedule)

    @staticmethod
    def _team_weeks(
        calendar: Sequence[SampleWeek], playoff_weeks: Sequence[int], roto: bool
    ) -> tuple[dict[str, TeamWeeks], float]:
        """Each team's games before, inside and after the playoff weeks, and the
        length in 7-day weeks of the season that is actually scored.

        Roto scores the whole calendar and has no playoffs; so does a league
        whose playoff weeks could not be placed.
        """
        if not calendar:
            return {}, 0.0
        first = min(playoff_weeks) if (playoff_weeks and not roto) else None
        last = max(playoff_weeks) if (playoff_weeks and not roto) else None
        counts: dict[str, list[int]] = {}
        scored_days = 0
        for week in calendar:
            if first is None or week.number < first:
                bucket = 0
            elif week.number <= last:
                bucket = 1
            else:
                bucket = 2
            if bucket < 2:
                scored_days += len(week.days)
            for day in week.days:
                for team in day:
                    counts.setdefault(team, [0, 0, 0])[bucket] += 1
        team_weeks = {team: TeamWeeks(regular=c[0], playoff=c[1], after=c[2]) for team, c in counts.items()}
        return team_weeks, scored_days / 7.0

    @staticmethod
    def _build_board(
        scoring: "ResolvedScoring",
        picked_ids: frozenset[int],
        my_ids: frozenset[int],
        inputs: BoardInputs,
        session: Optional[BoardSession] = None,
    ) -> DraftBoardResp:
        session = session or BoardSession()
        cat_defs = rankable_categories(scoring) if scoring.is_categories else []
        rank_type = DraftBoardService._rank_type(scoring)

        picked = picked_ids | inputs.session_picked
        mine = my_ids | inputs.session_mine
        removed = picked | mine

        entries = DraftBoardService.rank_pool(scoring, inputs, cat_defs, session)
        _model, playoffs = DraftBoardService.league_model(scoring, inputs, session, cat_defs)

        primary = DraftBoardService._primary_positions(inputs)
        eligible = DraftBoardService._eligible_slots(inputs)
        limits = DraftBoardService._position_limits(scoring)
        roster_slots = DraftBoardService._roster_slots(scoring)
        cap_check = DraftBoardService._cap_check(limits, mine, primary, inputs.positions)

        # What this roster is short of and what it has conceded: the weights the
        # fit column is scored with (None for points leagues, which have no
        # per-category z's to weigh).
        fit = DraftBoardService._fit_model(scoring, session, entries, mine, cat_defs, inputs)
        fit_values = DraftBoardService._fit_values(fit, entries)
        fit_ranks = DraftBoardService._fit_ranks(fit_values, removed)
        league_size = DraftBoardService._league_size(scoring, session)
        horizon = DraftBoardService._availability_horizon(session)

        # Whose board this is — decided before a row exists, because every row's
        # `board_rank` is that decision applied to one player.
        has_market = any(market_rank_of(m, rank_type) is not None for m in inputs.market.values())
        basis, basis_reason = rank_basis_for(
            scoring.league, session.espn_league_id, has_market, requested=session.board_source
        )

        rows: list[DraftBoardRow] = []
        candidates: list[dict] = []
        for cv_rank, entry in enumerate(entries, start=1):
            row, value = entry.row, entry.value
            market = inputs.market.get(row.id, {})
            market_rank = market_rank_of(market, rank_type)
            blocked = cap_check(row.id)
            team = inputs.current_team.get(row.id) or row.team
            po = playoffs.teams.get(team) if (playoffs is not None and team) else None
            if row.id not in removed:
                rows.append(DraftBoardRow(
                    player_id=row.id,
                    espn_id=row.espn_id,
                    name=row.name,
                    team=team,
                    position=inputs.positions.get(row.id),
                    primary_position=primary.get(row.id),
                    positions=eligible.get(row.id),
                    injury_status=DraftBoardService._injury_of(market),
                    cv_rank=cv_rank,
                    board_rank=(market_rank if basis == "espn" else cv_rank if basis == "cv" else None),
                    value=value,
                    value_source=inputs.source.get(row.id, "baseline"),
                    value_season=inputs.value_season.get(row.id),
                    last_season_gp=inputs.last_season_gp.get(row.id),
                    projected_gp=inputs.projected_gp.get(row.id),
                    season_games=entry.games,
                    playoff_games=po.games if po else None,
                    playoff_light_games=po.light if po else None,
                    playoff_games_by_week=list(po.per_week) if po else None,
                    fpts_avg=row.fpts_avg,
                    market_rank=market_rank,
                    adp=market.get("adp"),
                    auction_value=market_auction_of(market, rank_type),
                    market_delta=(market_rank - cv_rank) if market_rank is not None else None,
                    fit_value=fit_values.get(row.id),
                    fit_rank=fit_ranks.get(row.id),
                    availability=DraftBoardService._availability_of(market, horizon, league_size, rank_type),
                    cap_blocked=blocked,
                    categories=entry.cats,
                    category_z=entry.z,
                    score=entry.z_sum,
                ))
            candidates.append({
                "id": row.id, "name": row.name, "value": value, "team": team,
                "market_rank": market_rank,
                "cv_rank": cv_rank,
                "source": inputs.source.get(row.id, "baseline"),
                "season_value": entry.season_value,
                "z": entry.z,
                "z_sum": entry.z_sum,
                # A projection that carries his games has priced his availability;
                # 0 is "nobody projects him", as it is for the value itself.
                "games_projected": bool(inputs.projected_gp.get(row.id)),
                "expected_games": entry.expected_games,
                "share": entry.share,
                "fit_value": fit_values.get(row.id),
                "position": primary.get(row.id),
                "available": row.id not in removed,
                "blocked": blocked,
                "injury": DraftBoardService._injury_of(market),
                "slots": eligible.get(row.id),
            })

        # Market-only rows: no value to rank by, only ESPN's opinion that they
        # are worth drafting. Under ESPN's basis that opinion is their place on
        # the board; under CV's they trail everything valued, in ESPN's order.
        for entry in inputs.market_only:
            if entry.id in removed:
                continue
            market = inputs.market.get(entry.id, {})
            market_rank = market_rank_of(market, rank_type)
            team = inputs.current_team.get(entry.id)
            po = playoffs.teams.get(team) if (playoffs is not None and team) else None
            rows.append(DraftBoardRow(
                player_id=entry.id,
                espn_id=entry.espn_id,
                name=entry.name,
                team=team,
                position=entry.position,
                primary_position=primary.get(entry.id),
                positions=eligible.get(entry.id),
                injury_status=DraftBoardService._injury_of(market),
                cv_rank=None,
                board_rank=(market_rank if basis == "espn" else None),
                value=None,
                value_source="market",
                value_season=None,
                last_season_gp=None,
                projected_gp=None,
                playoff_games=po.games if po else None,
                playoff_light_games=po.light if po else None,
                playoff_games_by_week=list(po.per_week) if po else None,
                fpts_avg=None,
                market_rank=market_rank,
                adp=market.get("adp"),
                auction_value=market_auction_of(market, rank_type),
                market_delta=None,
                fit_value=None,
                fit_rank=None,
                availability=DraftBoardService._availability_of(market, horizon, league_size, rank_type),
                cap_blocked=cap_check(entry.id),
                categories=None,
                category_z=None,
                score=None,
            ))

        # What every candidate is worth in this room, then what this roster
        # would bench on its real game nights, measured once; every candidate's
        # congestion term is a delta against it.
        punts = DraftBoardService._room_values(candidates, fit)
        # Measured on the weeks the league actually scores: a benched night
        # after the fantasy season ends costs nothing.
        last_scored = max(playoffs.window.weeks) if playoffs is not None else None
        scored_weeks = tuple(
            w for w in inputs.schedule_weeks if last_scored is None or w.number <= last_scored
        )
        unscored = len(inputs.schedule_weeks) - len(scored_weeks)
        congestion = build_congestion_model(
            [DraftBoardService._congestion_player(c) for c in candidates if c["id"] in mine],
            roster_slots, scored_weeks,
            (inputs.season_weeks - unscored) if inputs.season_weeks else DEFAULT_SEASON_WEEKS,
        )
        terms = DraftBoardService._room_terms(candidates, scoring, session, fit, congestion, punts)
        # The room score on every row it was computed for: the `my_team` order,
        # and the same number the strip's cards decompose.
        room = {t.c["id"]: (place, t.score) for place, t in enumerate(terms, start=1)}
        for row in rows:
            if row.player_id in room:
                row.room_rank, row.room_score = room[row.player_id]
                if basis == "my_team":
                    row.board_rank = row.room_rank

        # One ordering for the whole board, valued and market-only rows alike.
        # Whoever the basis does not rank trails everyone it does, in the other
        # opinion's order — never lost, never promoted.
        if basis == "espn":
            rows.sort(key=lambda r: (r.market_rank is None, r.market_rank or 0,
                                     r.cv_rank is None, r.cv_rank or 0))
        elif basis == "my_team":
            rows.sort(key=lambda r: (r.room_rank is None, r.room_rank or 0,
                                     r.cv_rank is None, r.cv_rank or 0,
                                     r.market_rank is None, r.market_rank or 0))
        else:
            rows.sort(key=lambda r: (r.cv_rank is None, r.cv_rank or 0,
                                     r.market_rank is None, r.market_rank or 0))

        # `espn` needs ranks to order by; without a snapshot it degrades to the
        # CV composite rather than returning nothing, and the meta says which ran.
        rank_source = session.rank_source if (session.rank_source == "cv" or has_market) else "cv"
        recommendations = DraftBoardService._recommend(terms, fit, rank_source, punts)

        # The caller's drafted players, with what the roster zone needs to place
        # them. Big-board order; the session's picks say when each was taken.
        roster = [
            DraftRosterEntry(
                player_id=c["id"], name=c["name"], team=c["team"],
                primary_position=c["position"], positions=c["slots"],
                value=c["value"], value_source=c["source"], injury_status=c["injury"],
            )
            for c in candidates if c["id"] in mine
        ] + [
            DraftRosterEntry(
                player_id=entry.id, name=entry.name, team=inputs.current_team.get(entry.id),
                primary_position=primary.get(entry.id), positions=eligible.get(entry.id),
                value=None, value_source="market",
                injury_status=DraftBoardService._injury_of(inputs.market.get(entry.id, {})),
            )
            for entry in inputs.market_only if entry.id in mine
        ]
        # A drafted player neither the pool nor the market snapshot carries —
        # synced, but with no projection, no qualifying baseline and no ESPN
        # rank — would otherwise be off the board AND absent from the roster,
        # leaving the zone unable to place a pick the session records. His
        # identity was already fetched for the cap check.
        placed = {entry.player_id for entry in roster}
        for pid in sorted(mine - placed):
            name, espn_id = inputs.names.get(pid, (None, None))
            if name is None:
                continue
            roster.append(DraftRosterEntry(
                player_id=pid, name=name, team=inputs.current_team.get(pid),
                primary_position=primary.get(pid), positions=eligible.get(pid),
                value=None, value_source="baseline",
                injury_status=DraftBoardService._injury_of(inputs.market.get(pid, {})),
            ))

        available = len(rows)
        if rows:
            message = f"Draft board fetched successfully ({available} available of {len(entries) + len(inputs.market_only)})"
        else:
            message = f"No {inputs.season} player data yet — the board opens on last season's baseline"
        return DraftBoardResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=rows,
            recommendations=recommendations,
            roster=roster,
            meta=DraftBoardMeta(
                season=inputs.season,
                format=scoring.format,
                value_kind=PlayerValueService.value_kind_for(scoring),
                pool_size=len(entries),
                available=available,
                projection_count=sum(1 for s in inputs.source.values() if s == "projection"),
                baseline_count=sum(1 for s in inputs.source.values() if s == "baseline"),
                market_only_count=len(inputs.market_only),
                projections_as_of=inputs.projections_as_of,
                market_as_of=inputs.market_as_of,
                rank_source=rank_source,
                rank_source_requested=session.rank_source,
                market_rank_type=rank_type,
                rank_basis=basis,
                rank_basis_reason=basis_reason,
                rank_basis_requested=session.board_source,
                session_id=session.session_id,
                league_size=league_size,
                roster_slots=roster_slots,
                position_source=("espn" if primary else ("coarse" if any(inputs.positions.values()) else "none")),
                position_limits=limits,
                categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
                # The keys actually weighing zero, which for a category league is
                # what "punted" means; a points league has none to apply and
                # simply echoes what the session stores.
                punts=(fit.punts if fit is not None else list(session.punts)),
                category_need=DraftBoardService._category_need(fit),
                pace_source=(fit.pace_source if fit is not None else None),
                seats_drafted=(fit.seats_drafted if fit is not None else 0),
                congestion=DraftBoardService._congestion_meta(congestion, len(terms)),
                playoffs=DraftBoardService._playoffs_meta(playoffs, session.playoff_weight),
                settings_synced=scoring.settings_synced if scoring.league is not None else None,
                # dd/td weights score 0 against aggregate lines; name them rather
                # than imply the league's weights were fully applied (the
                # RankingsService._league_scoring rule).
                unsupported=([k for k in GAME_ONLY_KEYS if k in scoring.points.weights]
                             if not scoring.is_categories and not inputs.game_rates else []),
                projection_source=inputs.projection_source,
            ),
        )

    # ---- positions -------------------------------------------------------------

    @staticmethod
    def _primary_positions(inputs: BoardInputs) -> dict[int, str]:
        """Player id -> ESPN primary position, from the market snapshot only.

        `default_position_id` is 1-based (1=PG ... 5=C). nba.players.position is
        nba_api-coarse and never a substitute — it is handled separately by the
        coarse cap fallback.
        """
        out: dict[int, str] = {}
        for pid, market in inputs.market.items():
            name = POSITION_ID_MAP.get(market.get("default_position_id") or 0)
            if name:
                out[pid] = name
        return out

    @staticmethod
    def _eligible_slots(inputs: BoardInputs) -> dict[int, list[str]]:
        """Player id -> ESPN lineup-slot names, verbatim.

        `eligible_slot_ids` are 0-based lineup-slot ids — a different space from
        `default_position_id` above. Bench and IR are dropped: they say nothing
        about where a player can start.
        """
        out: dict[int, list[str]] = {}
        for pid, market in inputs.market.items():
            slot_ids = market.get("eligible_slot_ids")
            if not slot_ids:
                continue
            names = [POSITION_MAP.get(int(s)) for s in slot_ids if isinstance(s, int)]
            usable = [n for n in names if n and n not in _NON_STARTING_SLOTS]
            if usable:
                out[pid] = usable
        return out

    # ---- position caps ---------------------------------------------------------

    @staticmethod
    def _position_limits(scoring: "ResolvedScoring") -> dict[str, int]:
        limits = getattr(scoring.league, "position_limits", None) if scoring.league is not None else None
        return dict(limits) if limits else {}

    @staticmethod
    def _roster_slots(scoring: "ResolvedScoring") -> dict[str, int]:
        """The lineup the room drafts for.

        A league's own, as synced. A room with no league at all — a mock, a
        manual room — fields ESPN's default, so its roster zone has seats to
        fill and the `my_team` order has a lineup to measure against. A league
        whose settings never synced is the one case left empty: its real
        lineup is unknown, and assuming the default would say otherwise.
        """
        if scoring.league is None:
            return dict(DEFAULT_ROSTER_SLOTS)
        slots = getattr(scoring.league, "roster_slots", None)
        return dict(slots) if slots else {}

    @staticmethod
    def _league_size(scoring: "ResolvedScoring", session: BoardSession) -> Optional[int]:
        """Teams in the draft: the session's pick order, else the league's."""
        if session.league_size:
            return session.league_size
        draft_settings = getattr(scoring.league, "draft_settings", None) if scoring.league is not None else None
        order = (draft_settings or {}).get("pick_order") or []
        return len(order) or None

    @staticmethod
    def _cap_check(
        limits: Mapping[str, int],
        my_ids: frozenset[int],
        primary: Mapping[int, str],
        coarse: Mapping[int, Optional[str]],
    ):
        """A predicate saying whether drafting a player would break a hard cap.

        ESPN counts caps by `defaultPositionId` — a player's primary position,
        not his eligibility (confirmed behaviorally, plan §8 #5). That is exact
        whenever the market snapshot knows every rostered player's primary
        position. When it does not (a roster player ESPN has no market row for,
        or a snapshot written before the pipeline captured positions), the check
        falls back to the coarse nba_api groups the old board used: a group is
        enforceable only when every ESPN position inside it is capped, so a
        split cap blocks nobody rather than blocking the wrong player.

        A candidate whose own ESPN position the league does not cap is answered
        outright, roster or no roster — that answer cannot depend on counting.
        """
        if not limits:
            return lambda pid: False

        # The exact rule is only available when every player already on the
        # roster has a known primary position — one unknown roster player and
        # the counts are wrong, so that candidate falls back to the coarse rule.
        roster_known = all(pid in primary for pid in my_ids)
        exact_counts = Counter(primary[pid] for pid in my_ids if pid in primary)

        coarse_caps = DraftBoardService._enforceable_caps(limits)
        coarse_counts = Counter(
            group for group in (DraftBoardService._primary_group(coarse.get(pid)) for pid in my_ids)
            if group is not None
        )

        def blocked(pid: int) -> bool:
            position = primary.get(pid)
            if position is not None:
                cap = limits.get(position)
                if cap is None:
                    # ESPN says this position is uncapped, and no roster
                    # composition can breach a cap that does not exist — so this
                    # answer needs no counting and never falls back to the coarse
                    # rule, which would let a PF listed 'C' by nba_api trip a
                    # centre cap the league never applied to him.
                    return False
                if roster_known:
                    return exact_counts[position] >= cap
            group = DraftBoardService._primary_group(coarse.get(pid))
            return group in coarse_caps and coarse_counts[group] >= coarse_caps[group]

        return blocked

    @staticmethod
    def _enforceable_caps(position_limits: Mapping[str, int]) -> dict[str, int]:
        """Caps summed into the coarse groups our position data can enforce.

        A group qualifies only when every ESPN position in it is capped; an
        explicit 0 is a real "none allowed" rule and sums like any other cap.
        """
        groups: dict[str, list[int]] = {}
        for pos, cap in (position_limits or {}).items():
            group = _COARSE_GROUP.get(str(pos).upper())
            if group is None:
                continue
            try:
                groups.setdefault(group, []).append(int(cap))
            except (TypeError, ValueError):
                continue
        return {g: sum(caps) for g, caps in groups.items() if len(caps) == _GROUP_SIZE[g]}

    @staticmethod
    def _primary_group(position: Optional[str]) -> Optional[str]:
        """A player's primary coarse position: the first segment of 'F-C' is F."""
        if not position:
            return None
        head = position.split("-", 1)[0].strip().upper()
        return head if head in _GROUP_SIZE else None

    @staticmethod
    def _rank_type(scoring: "ResolvedScoring") -> str:
        """Which of ESPN's two boards this league drafts off."""
        return rank_type_for(scoring.is_categories)

    @staticmethod
    def _injury_of(market: Mapping) -> Optional[str]:
        """The market row's injury status, with "healthy" reported as nothing."""
        status = market.get("injury_status")
        if not status or str(status).upper() in ("ACTIVE", "NORMAL"):
            return None
        return str(status)

    # ---- congestion ------------------------------------------------------------

    @staticmethod
    def _congestion_player(c: Mapping) -> CongestionPlayer:
        """A candidate dict as the matching sees him: his per-game value in this
        room floored at zero, ESPN slots when the market knows them, primary
        position as the fallback, and the share of his team's games he plays."""
        slots = c.get("slots")
        value = c.get("room_per_game", c.get("value"))
        return CongestionPlayer(
            id=c["id"],
            value=max(float(value), 0.0) if value is not None else 0.0,
            team=c.get("team"),
            slots=frozenset(slots) if slots else None,
            position=c.get("position"),
            share=float(c.get("share", 1.0)),
        )

    @staticmethod
    def _congestion_detail(pen: Penalty) -> str:
        """One line on what the term measured, or why it could not."""
        if pen.reason:
            return pen.reason
        if pen.games == 0:
            return f"no games on the calendar for {pen.team}"
        shared = f"; {pen.stack + 1} would share {pen.team}'s schedule" if pen.stack else ""
        if pen.value < 0:
            weeks = f"{pen.weeks} week" + ("s" if pen.weeks != 1 else "")
            return f"would bench ~{pen.per_week:.1f}/week of starter value over {weeks}" + shared
        return "fits the lineup every game night" + shared

    @staticmethod
    def _congestion_meta(model: CongestionModel, pool: int) -> DraftCongestionResp:
        """The roster-level summary the roster zone renders; `pool` is how many
        candidates the room scored."""
        return DraftCongestionResp(
            benched_per_week=model.benched_per_week,
            benched_season=model.benched_season,
            sample_weeks=list(model.sample_weeks),
            season_weeks=model.season_weeks if model.weeks else 0,
            slots=len(model.slots),
            stacks=[
                DraftStackResp(team=s.team, count=s.count, player_ids=list(s.player_ids))
                for s in model.stacks
            ],
            no_team=list(model.no_team),
            evaluated=pool if model.active else 0,
        )

    # ---- playoffs --------------------------------------------------------------

    @staticmethod
    def _playoffs_meta(
        playoffs: Optional[PlayoffSchedule], weight: float
    ) -> Optional[DraftPlayoffsResp]:
        if playoffs is None or not playoffs.teams:
            return None
        low, high = playoffs.span
        return DraftPlayoffsResp(
            weeks=list(playoffs.window.weeks),
            rounds=[list(r) for r in playoffs.window.round_weeks],
            label=playoffs.window.label,
            source=playoffs.window.source,
            weight=weight,
            weights=list(PLAYOFF_WEIGHTS),
            games_min=low,
            games_max=high,
            games_mean=round(playoffs.mean_games, 1),
        )

    # ---- category fit ----------------------------------------------------------

    @staticmethod
    def _fit_model(
        scoring: "ResolvedScoring",
        session: BoardSession,
        entries: list,
        my_ids: frozenset[int],
        cat_defs: list,
        inputs: BoardInputs,
    ) -> Optional[FitModel]:
        """The weights this roster's fit column is scored with.

        Points leagues get None: `value` is already the league's own scoring,
        and there are no per-category z's to re-weight.
        """
        if not scoring.is_categories or not cat_defs:
            return None
        roster_size = session.rounds or rounds_from_roster_slots(
            DraftBoardService._roster_slots(scoring)
        )
        tier_size = draftable_tier_size(
            DraftBoardService._league_size(scoring, session), roster_size
        )
        ranked = [(entry.row.id, entry.z) for entry in entries]
        return build_fit_model(
            ranked, my_ids, cat_defs, tier_size, session.punts,
            opponent_rosters=DraftBoardService._opponent_rosters(inputs, session),
        )

    @staticmethod
    def _opponent_rosters(
        inputs: BoardInputs, session: BoardSession
    ) -> list[frozenset[int]]:
        """What every seat but mine has drafted.

        None of them without a confirmed slot: my own roster would be counted
        among the teams I am measured against, which would flatter every
        category I am strong in and hide every hole. The tier estimate is the
        right answer until the room knows which seat is mine.
        """
        if session.my_slot is None or not inputs.seat_players:
            return []
        return [
            players for seat, players in sorted(inputs.seat_players.items())
            if seat != session.my_slot and players
        ]

    @staticmethod
    def _fit_values(fit: Optional[FitModel], entries: list) -> dict[int, float]:
        """Player id -> roster-specific value, on the same scale as `value`."""
        if fit is None:
            return {}
        return {
            entry.row.id: category_value(fit.fit_z(entry.z_sum, entry.z))
            for entry in entries
        }

    @staticmethod
    def _fit_ranks(fit_values: Mapping[int, float], removed: frozenset[int]) -> dict[int, int]:
        """Rank by fit among the players still available.

        Available-only on purpose: fit answers "who is best for this roster
        now", so a drafted player holding rank 3 would be noise. Ties keep the
        balanced order (`fit_values` is built in it) rather than shuffling
        between reads.
        """
        if not fit_values:
            return {}
        order = {pid: i for i, pid in enumerate(fit_values)}
        available = sorted(
            ((pid, value) for pid, value in fit_values.items() if pid not in removed),
            key=lambda pair: (-pair[1], order[pair[0]]),
        )
        return {pid: rank for rank, (pid, _value) in enumerate(available, start=1)}

    @staticmethod
    def _category_need(fit: Optional[FitModel]) -> list[CategoryNeedResp]:
        if fit is None:
            return []
        return [
            CategoryNeedResp(
                key=need.key, label=need.label, mine=need.mine, pace=need.pace,
                need=need.need, weight=need.weight, punted=need.punted,
                my_rank=need.my_rank, seats=need.seats,
            )
            for need in fit.needs
        ]

    # ---- availability ----------------------------------------------------------

    @staticmethod
    def _availability_horizon(session: BoardSession) -> Optional[int]:
        """The pick to ask "will he still be here?" about.

        My next turn — except while I am on the clock, where every player on
        the board is available *now* by definition and the only useful question
        is whether waiting one more turn is safe. None when I have no turn left
        to wait for, which makes the question meaningless rather than urgent.
        """
        if session.my_next_pick is None:
            return None
        if session.draft_front is not None and session.my_next_pick <= session.draft_front:
            return session.my_following_pick
        return session.my_next_pick

    @staticmethod
    def _availability_of(
        market: Mapping, horizon: Optional[int], league_size: Optional[int],
        rank_type: str = "standard",
    ) -> Optional[str]:
        """Likely / toss-up / gone, from where the market drafts him vs `horizon`.

        Crowd ADP first (it is what other managers actually did), ESPN's
        editorial rank as the fallback. A bucket, never a percentage: one point
        estimate cannot support a probability, and a number that looks
        calibrated would be read as one.
        """
        if horizon is None or not league_size:
            return None
        expected = market.get("adp")
        if expected is None:
            expected = market_rank_of(market, rank_type)
        if expected is None:
            return None

        threshold = max(AVAILABILITY_MIN_THRESHOLD, round(league_size / 2))
        gap = float(expected) - horizon
        if gap >= threshold:
            return "likely"
        if gap <= -threshold:
            return "gone"
        return "tossup"

    # ---- the room score ----------------------------------------------------------

    @staticmethod
    def _eligible_at(c: Mapping, position: str) -> bool:
        """Whether a candidate can fill a seat only `position` fills: ESPN's own
        lineup slots when the market knows them, his primary position otherwise."""
        slots = c.get("slots")
        return position in slots if slots else c.get("position") == position

    @staticmethod
    def _tier_left(candidates: list[dict], size: int, key: str = "season_value") -> int:
        """How many of the top `size` candidates by `key` are undrafted."""
        if size <= 0:
            return 0
        top = sorted(candidates, key=lambda c: -c[key])[:size]
        return sum(1 for c in top if c["available"])

    @staticmethod
    def _replacement_levels(
        candidates: list[dict], pool: list[dict], roster_slots: Mapping[str, int],
        league_size: int, key: str,
    ) -> tuple[float, dict[str, float]]:
        """The replacement level a candidate is measured against, in `key`
        values: the league's last starter, and — only for a position that is
        genuinely short — the lower level at that position.

        A basketball lineup is mostly seats anyone can fill: G, F and three UT
        beside one seat each for the five positions. The players a league
        starts are therefore, to a first approximation, simply its best
        `league_size x starting seats`, whatever they play — a second centre
        starts at UT — and the replacement level is the last of them. Giving
        each position its own level, as a sport with rigid lineups would, hands
        whichever position ESPN lists fewest good players at a premium nothing
        in the lineup earns: on the 2026-27 board it moved power forwards up 35
        places and centres down 30 before a pick was made.

        A position does earn one when the seats *only it* can fill outnumber
        the players worth starting in them: its last dedicated starter then
        sits below the league's, and that lower level is the bar for anyone
        who can take such a seat. That is positional scarcity in value units,
        and in an ordinary league it is simply absent. It also ends when the
        seats are filled: once every dedicated starter at a position has been
        drafted there is no seat left for the next one to take, and he is
        measured against the league like everybody else.

        Both levels are the *marginal* starter still to be filled: the tier is
        fixed against the full pool, drafted or not, and the count of it still
        undrafted is indexed into the players available. Indexing the original
        need instead would slide deeper into the distribution as the top came
        off the board and make the bar FALL through the draft; a tier
        recomputed over survivors would refill itself from below after every
        pick and never run dry. Once a tier is gone the bar is the best player
        left, so the survivors of a run are worth what they are, not a premium.
        """
        seats = active_slots(roster_slots)
        # A league whose lineup never synced is measured against ESPN's default size.
        starting = len(seats) if roster_slots else DEFAULT_STARTERS
        need = league_size * starting
        available = sorted((c[key] for c in pool), reverse=True)
        if not available or need <= 0:
            return 0.0, {}
        left = DraftBoardService._tier_left(candidates, need, key)
        overall = available[min(left, len(available) - 1)]

        short: dict[str, float] = {}
        for position in ESPN_POSITIONS:
            dedicated = league_size * seats.count(position)
            if dedicated <= 0:
                continue
            eligible = [c for c in candidates if DraftBoardService._eligible_at(c, position)]
            values = sorted(
                (c[key] for c in pool if DraftBoardService._eligible_at(c, position)), reverse=True
            )
            if not values:
                continue
            still_to_fill = DraftBoardService._tier_left(eligible, dedicated, key)
            if still_to_fill <= 0:
                continue
            bar = values[min(still_to_fill, len(values) - 1)]
            if bar < overall:
                short[position] = bar
        return overall, short

    @staticmethod
    def _bar_for(c: Mapping, overall: float, short: Mapping[str, float]) -> tuple[float, Optional[str]]:
        """A candidate's replacement level, and the short position it came from, if any."""
        bar, position = overall, None
        for candidate_position, level in short.items():
            if level < bar and DraftBoardService._eligible_at(c, candidate_position):
                bar, position = level, candidate_position
        return bar, position

    @staticmethod
    def _room_values(candidates: list[dict], fit: Optional[FitModel]) -> list[str]:
        """Give every candidate his value *in this room* and return the punts applied.

        Court Vision's value counts every category the league scores. A room
        that has conceded some does not: `room_value` is the same season value
        with the punted categories left out of the sum, and `room_per_game` the
        per-game number the lineup matching weighs him by. With nothing punted —
        and in a points league, which has nothing to punt — they are the balanced
        values, exactly.
        """
        punts = fit.punts if fit is not None else []
        for c in candidates:
            z = c.get("z")
            if punts and z is not None and c.get("z_sum") is not None:
                kept = float(c["z_sum"]) - sum(float(z.get(key, 0.0)) for key in punts)
                c["room_per_game"] = category_value(kept)
                c["room_value"] = round(c["room_per_game"] * DEFAULT_GAMES, VALUE_DECIMALS)
            else:
                c["room_per_game"] = c["value"]
                c["room_value"] = c["season_value"]
        return list(punts)

    @staticmethod
    def _room_terms(
        candidates: list[dict],
        scoring: "ResolvedScoring",
        session: BoardSession,
        fit: Optional[FitModel] = None,
        congestion: Optional[CongestionModel] = None,
        punts: Sequence[str] = (),
    ) -> list[_Terms]:
        """Every player this roster could still draft, scored for this room, best first.

        One currency — season value in the league's own scoring — and four terms:

            score = value over replacement + punts + injury + congestion

        `punts` is how the room's conceded categories move his value over
        replacement (his own value and the replacement level both move),
        `injury` only prices what the projection could not — where his games
        are projected, his availability is already in his value — and
        `congestion` charges back the starts this roster could not use. What
        the roster is short of is deliberately not a term: weighting categories
        by need lost to leaving them alone in the redraft experiments
        (`experiments/ranking_engine`), so it stays on the card as information.

        Expects `_room_values` to have run over `candidates`. The same list
        orders the recommendation strip and the `my_team` board, so the strip
        is always the top of that board.
        """
        pool = [c for c in candidates if c["available"] and not c["blocked"]]
        if not pool:
            return []

        roster_slots = DraftBoardService._roster_slots(scoring)
        league_size = DraftBoardService._league_size(scoring, session) or 0
        overall, short = DraftBoardService._replacement_levels(
            candidates, pool, roster_slots, league_size, "season_value"
        )
        # In the room's own values when it punts: the replacement level moves
        # with a punt as surely as the candidate does, and it is the difference
        # between the two that is worth anything.
        room_overall, room_short = (
            DraftBoardService._replacement_levels(candidates, pool, roster_slots, league_size, "room_value")
            if punts else (overall, short)
        )

        terms: list[_Terms] = []
        for c in pool:
            season_value, room_value = c["season_value"], c["room_value"]
            bar, bar_position = DraftBoardService._bar_for(c, overall, short)
            room_bar, _ = DraftBoardService._bar_for(c, room_overall, room_short)
            vorp = round(season_value - bar, VALUE_DECIMALS)
            punted = round((room_value - room_bar) - vorp, VALUE_DECIMALS) if punts else 0.0

            # Injury is priced once. A projection that carries his games has
            # already charged for the ones he will miss; the flat discount is
            # for a player whose games nothing projects.
            penalty = INJURY_PENALTY.get(str(c["injury"]).upper(), 0.0) if c["injury"] else 0.0
            injury = (
                0.0 if c.get("games_projected")
                else -round(penalty * max(room_value, 0.0), VALUE_DECIMALS) or 0.0
            )

            # What the roster is short of, as information: how far the
            # need-weighted fit sits from the room's own value.
            fit_value = c.get("fit_value")
            category_fit = (
                round((fit_value - c["room_per_game"]) * DEFAULT_GAMES, VALUE_DECIMALS)
                if fit_value is not None else 0.0
            )

            terms.append(_Terms(
                c=c, position=c["position"], season_value=season_value, room_value=room_value,
                bar=bar, room_bar=room_bar, bar_position=bar_position,
                league_size=league_size, vorp=vorp, punts=punted,
                injury=injury, category_fit=category_fit,
            ))

        # Congestion last, and for everyone: the `my_team` board is ordered by
        # the whole score, and the roster's lineup is measured once, so asking
        # what each candidate would do to it is a lookup per game night.
        for t in terms:
            if congestion is not None:
                pen = congestion.penalty(DraftBoardService._congestion_player(t.c))
                t.congestion, t.congestion_detail = pen.value, DraftBoardService._congestion_detail(pen)
            else:
                t.congestion_detail = "not measured"
            # One rounding over the already-rounded terms, added in the order
            # the components list them, so summing the visible terms
            # reproduces the score.
            t.score = round(t.vorp + t.punts + t.injury + t.congestion, VALUE_DECIMALS)
        terms.sort(key=lambda t: (-t.score, -t.season_value, t.c.get("cv_rank") or 0))
        return terms

    @staticmethod
    def _recommend(
        terms: list[_Terms],
        fit: Optional[FitModel] = None,
        rank_source: str = "cv",
        punts: Sequence[str] = (),
    ) -> list[DraftRecommendation]:
        """The best of what is left, ordered by `rank_source`.

        Every term is computed either way — an ESPN-ordered list still carries
        CV's whole decomposed score, which is what makes the two views
        comparable at a glance instead of two unrelated lists. Anyone ESPN does
        not rank sorts after everyone he does, by CV score — not silently
        dropped, just never preferred to a ranked player.
        """
        if rank_source == "espn":
            chosen = sorted(
                terms, key=lambda t: (t.c.get("market_rank") is None, t.c.get("market_rank") or 0, -t.score)
            )
        else:
            chosen = terms
        return [
            DraftBoardService._recommendation(t, fit, rank_source, punts)
            for t in chosen[:RECOMMENDATION_COUNT]
        ]

    @staticmethod
    def _recommendation(
        t: _Terms, fit: Optional[FitModel], rank_source: str = "cv", punts: Sequence[str] = (),
    ) -> DraftRecommendation:
        """One candidate with the whole score decomposed."""
        c = t.c
        components = [
            RecommendationComponent(
                key="season_value", label="Season value", value=t.season_value, in_score=False,
                detail=f"{c['value']} per game over a projected season",
            ),
            RecommendationComponent(
                key="vorp", label="Value over replacement", value=t.vorp, in_score=True,
                detail=DraftBoardService._replacement_detail(t),
            ),
        ]
        if fit is not None:
            components.append(RecommendationComponent(
                key="punts", label="Punted categories", value=t.punts, in_score=True,
                detail=DraftBoardService._punt_detail(fit, punts, t),
            ))
        components += [
            RecommendationComponent(
                key="injury", label="Injury risk", value=t.injury, in_score=True,
                detail=DraftBoardService._injury_detail(c),
            ),
            RecommendationComponent(
                key="congestion", label="Lineup congestion", value=t.congestion, in_score=True,
                detail=t.congestion_detail,
            ),
        ]
        if fit is not None:
            components.append(RecommendationComponent(
                key="category_fit", label="Category fit", value=t.category_fit, in_score=False,
                detail=DraftBoardService._fit_detail(fit, c.get("z")),
            ))
        return DraftRecommendation(
            player_id=c["id"],
            name=c["name"],
            primary_position=t.position,
            value=c["value"],
            season_value=t.season_value,
            vorp=t.vorp,
            score=t.score,
            source=rank_source,
            market_rank=c.get("market_rank"),
            cv_rank=c.get("cv_rank"),
            components=components,
            reason=DraftBoardService._reason(
                c["name"], t.bar_position, t.vorp, t.punts, t.injury,
                t.congestion, c["injury"], rank_source, c.get("market_rank"),
            ),
        )

    @staticmethod
    def _replacement_detail(t: _Terms) -> str:
        """What he was measured against, and why that level."""
        bar = round(t.bar, VALUE_DECIMALS)
        if t.bar_position:
            return (
                f"replacement at {t.bar_position} is {bar} — below the league's last starter: "
                f"startable {t.bar_position} are short"
            )
        if not t.league_size:
            return "no league size on file — measured from zero"
        return f"replacement is {bar}, the last starter in a {t.league_size}-team league"

    @staticmethod
    def _injury_detail(c: Mapping) -> str:
        """What the injury term did, and — when it did nothing — why not."""
        if not c["injury"]:
            return "no injury flag"
        if c.get("games_projected"):
            return f"listed {c['injury']} — already in his {c['expected_games']:.0f} projected games"
        return f"listed {c['injury']}"

    @staticmethod
    def _punt_detail(fit: FitModel, punts: Sequence[str], t: _Terms) -> str:
        """Which categories the room conceded, and what leaving them out moved."""
        if not punts:
            return "nothing punted — every category counts"
        labels = {need.key: need.label for need in fit.needs}
        named = ", ".join(labels.get(key, key) for key in punts)
        own = round(t.room_value - t.season_value, VALUE_DECIMALS)
        bar = round(t.room_bar - t.bar, VALUE_DECIMALS)
        return f"without {named}: his value {own:+.1f}, replacement {bar:+.1f}"

    @staticmethod
    def _fit_detail(fit: Optional[FitModel], z: Optional[Mapping[str, float]]) -> str:
        """Which categories this roster is short of that the candidate moves.

        Information, not a term: the two largest need-weighted movers, in the
        same season-value points as everything else on the card. Punted
        categories are left out — those are in the score, under their own term.
        They can fall a little short of summing to the component: the value
        scale is clamped at zero, so for a player below the floor part of the
        shift has nowhere to land.
        """
        if fit is None:
            return "points league — value is already this league's own scoring"
        drivers = [(need, shift) for need, shift in fit.drivers(z) if not need.punted][:2]
        if not drivers:
            return "balanced: no category need pulls this pick either way"
        parts = []
        for need, shift in drivers:
            amount = shift * CATEGORY_VALUE_SCALE * DEFAULT_GAMES
            why = f"{abs(need.need):.1f}σ {'behind' if need.need > 0 else 'ahead of'} pace"
            parts.append(f"{amount:+.1f} {need.label} ({why})")
        return ", ".join(parts) + " — not in the score"

    @staticmethod
    def _reason(
        name: str,
        short_position: Optional[str],
        vorp: float,
        punts: float,
        injury: float,
        congestion: float,
        injury_status: Optional[str],
        rank_source: str = "cv",
        market_rank: Optional[int] = None,
    ) -> str:
        """One sentence naming what actually drove the pick.

        Under `espn` that is ESPN's rank — the reason must name the thing doing
        the ordering, or the room reads a CV rationale for a pick CV did not
        make. Under `cv` it is the terms, and the sentence ends with where ESPN
        has him: the board is ordered by ESPN, so a CV pick is only readable
        next to the number it disagrees with.
        """
        where = f" at {short_position}" if short_position else ""
        place = f"ESPN has him #{market_rank}" if market_rank is not None else "unranked by ESPN"
        if rank_source == "espn":
            top = f"ESPN's #{market_rank}" if market_rank is not None else "unranked by ESPN"
            return f"{name}: {top} and the best left on their board; CV has him {vorp:+.1f} over replacement{where}"
        parts = [f"{vorp:+.1f} over replacement{where}"]
        if punts:
            parts.append(f"{punts:+.1f} for your punts")
        if injury:
            parts.append(f"{injury:+.1f} for {injury_status}")
        if congestion:
            parts.append(f"{congestion:+.1f} for lineup congestion")
        return f"{name}: " + ", ".join(parts) + f"; {place}"

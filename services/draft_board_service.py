"""
Draft Lab board: every draftable player, valued by one league's scoring, plus
recommendations for the caller's next pick with every component visible.

Composes what already exists rather than inventing a new engine:

- Pool:   ESPN's published per-game projections (nba.player_projections, latest
          snapshot) where present, union each player's final previous-season row
          (services.scoring.pool.load_baseline_pool) for everyone else, union
          market-only rows for players ESPN drafts that neither can value — a
          rookie is on the board from the day ESPN ranks him, with `value: null`
          and `value_source: market`, and upgrades in place when projections land.
- Value:  the same dispatcher math every provider uses — the league's point
          weights for points leagues, the fpts-scale category value
          (services.scoring.category_value) for category leagues.
- Market: ESPN editorial draft rank / auction value and crowd ADP from
          nba.draft_market (latest snapshot), joined by player id, with a
          `market_rank − cv_rank` delta.
- Position: `default_position_id` (ESPN primary, 1-based) and `eligible_slot_ids`
          (0-based lineup slots) off the same snapshot. The two id spaces are
          kept apart: primary position drives caps and replacement level,
          eligibility drives the flexibility bonus.
- Caps:   hard per-position roster caps from usr.leagues.position_limits mark
          candidates the caller can no longer draft (flagged, never hidden, and
          never recommended).

- Fit:    category leagues also carry a second, roster-specific value
          (services.draft_fit): the same per-category z's re-weighted by how
          far this roster trails an average team, with punted categories at
          zero. `value` says what a player is worth to anyone, `fit_value`
          what he is worth here; both map through the same scale.

cv_rank is computed over the FULL pool, picked players included, so it reads as
a pre-draft big-board rank: it stays stable as picks remove rows and remains
comparable to market_rank all draft long. `fit_rank` deliberately does not: it
ranks what is still available, and moves with every pick.

One `run_db` fetch materializes every input; one `run_cpu` call scores and
assembles the response (the rankings-service split — z-scoring a pool and
building hundreds of pydantic rows must not hold a DB permit).

Recommendations (v1) rank the available, non-cap-blocked pool by

    score = vorp + scarcity + flexibility + category_fit − injury

each term expressed in season-value points so the sum is interpretable, and each
returned alongside the score. `season_value` (value × projected games) is the
base the rest are computed from and rides along as a non-summed component. The
model is deliberately simple: the visible breakdown matters more than its
sophistication this season.

Availability answers "will he still be there when I pick again?" as a bucket —
likely / toss-up / gone — from the gap between ESPN's ADP and the caller's next
turn. ESPN publishes a point estimate, not a distribution, so a percentage
would imply a calibration that does not exist (plan diff #6).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

from core.compute import run_cpu
from core.settings import settings
from db import base as db_base
from db.models.drafts import DraftPick
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.players import Player
from schemas.common import ApiStatus, CategoryDefResp
from schemas.draft import (
    CategoryNeedResp,
    DraftBoardMeta,
    DraftBoardResp,
    DraftBoardRow,
    DraftRecommendation,
    DraftRosterEntry,
    RecommendationComponent,
)
from services.draft_fit import FitModel, build_fit_model, draftable_tier_size
from services.draft_service import (
    draft_front,
    next_pick_for_slot,
    resolve_lagging_picks,
    rounds_from_roster_slots,
)
from services.player_value_service import PlayerValueService
from services.rankings_service import GAME_ONLY_KEYS
from services.scoring.category_rank import PoolRow, compute_category_scores
from services.scoring.category_value import (
    CATEGORY_VALUE_SCALE,
    category_value,
    rankable_categories,
)
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.pool import load_baseline_pool
from services.scoring.providers.espn_settings import POSITION_ID_MAP
from utils.espn_helpers import POSITION_MAP

if TYPE_CHECKING:  # pragma: no cover
    from api.deps import OwnedDraftSessionContext
    from services.scoring.resolver import ResolvedScoring

# ESPN cap position -> the coarse nba_api position group it belongs to, and how
# many ESPN positions each group holds (see `_enforceable_caps`).
_COARSE_GROUP: dict[str, str] = {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}
_GROUP_SIZE: dict[str, int] = {"G": 2, "F": 2, "C": 1}

# The five ESPN positions a player can be primary at, in default_position_id order.
ESPN_POSITIONS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")

# Lineup slots that hold a starter but are not a position: everyone is UT-eligible,
# so UT is neither a flexibility bonus nor a replacement-level pool of its own.
_UNIVERSAL_SLOTS = frozenset({"UT"})
_NON_STARTING_SLOTS = frozenset({"BE", "IR", "Rookie", ""})

# Which ESPN positions can fill a multi-position lineup slot. Used only to spread
# a league's derived slots (G, F, UT, ...) across the five primary positions when
# computing how many starters the league needs at each.
_SLOT_MEMBERS: dict[str, tuple[str, ...]] = {
    "PG": ("PG",), "SG": ("SG",), "SF": ("SF",), "PF": ("PF",), "C": ("C",),
    "G": ("PG", "SG"), "F": ("SF", "PF"),
    "SG/SF": ("SG", "SF"), "G/F": ("PG", "SG", "SF", "PF"),
    "PF/C": ("PF", "C"), "F/C": ("SF", "PF", "C"),
    "UT": ESPN_POSITIONS,
}

VALUE_DECIMALS = 1

# Games a player is assumed to play when nothing projects him. Roughly a healthy
# rotation season; it scales every candidate the same way, so it moves the size
# of the numbers, not the order.
DEFAULT_PROJECTED_GP = 65

# Recommendation weights. Small on purpose — VORP does the work, the rest are
# tiebreakers whose job is to be legible.
SCARCITY_WEIGHT = 0.5        # at most half a player's VORP again, at a dry position
SCARCITY_IDLE_NEED = 0.25    # damping when my own roster does not need the position
FLEX_RATE = 0.02             # per extra startable lineup slot, as a share of season value
RECOMMENDATION_COUNT = 5

# How far ADP has to sit from the pick in question before the answer stops
# being "it depends". Half a round of picks, floored so a tiny league still
# leaves room for a toss-up band.
AVAILABILITY_MIN_THRESHOLD = 3

# ESPN injuryStatus -> the share of season value a candidate is discounted by.
# ACTIVE (and anything unknown) is no discount at all.
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
    draft_front: Optional[int] = None       # one past the last pick made on the clock
    my_next_pick: Optional[int] = None      # my next turn, counted from the front
    my_following_pick: Optional[int] = None  # and the turn after that

    @classmethod
    def of(cls, ctx: "OwnedDraftSessionContext") -> "BoardSession":
        return cls(
            session_id=ctx.session_id,
            my_slot=ctx.my_slot,
            rounds=ctx.rounds,
            league_size=ctx.league_size,
            draft_type=ctx.draft_type,
            punts=tuple(ctx.punts),
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
    last_season_gp: dict[int, int] = field(default_factory=dict)    # players with a baseline row
    projected_gp: dict[int, Optional[int]] = field(default_factory=dict)
    projections_as_of: Optional[date] = None
    market: dict[int, dict] = field(default_factory=dict)           # player id -> market + position fields
    market_as_of: Optional[date] = None
    market_only: list[MarketOnlyRow] = field(default_factory=list)  # ranked, unvaluable players
    positions: dict[int, Optional[str]] = field(default_factory=dict)   # nba_api coarse position
    names: dict[int, tuple[str, Optional[int]]] = field(default_factory=dict)   # id -> (name, espn_id)
    session_picked: frozenset[int] = frozenset()        # drafted by anyone, from usr.draft_picks
    session_mine: frozenset[int] = frozenset()          # drafted by the caller
    used_picks: tuple[int, ...] = ()                    # pick numbers recorded in the session
    keeper_picks: tuple[int, ...] = ()                  # the subset spent before the draft started


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
        if session_id is not None:
            picks = list(
                DraftPick.select(
                    DraftPick.player, DraftPick.by_me, DraftPick.espn_player_id,
                    DraftPick.player_name, DraftPick.overall_pick, DraftPick.source,
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

        baseline = {row.id: row for row in load_baseline_pool()}
        pool: dict[int, PoolRow] = dict(baseline)
        source = {pid: "baseline" for pid in baseline}
        last_season_gp = {pid: row.gp for pid, row in baseline.items()}

        projections_as_of, projections = DraftBoardService._latest_projections(season)
        projected_gp: dict[int, Optional[int]] = {}
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
            projected_gp[rec.player_id] = int(rec.projected_gp) if rec.projected_gp is not None else None

        market: dict[int, dict] = {}
        market_as_of: Optional[date] = None
        for rec in DraftMarket.latest_for_season(season):
            market_as_of = rec.as_of_date
            market[rec.player_id] = {
                "overall_rank": int(rec.overall_rank) if rec.overall_rank is not None else None,
                "adp": round(float(rec.adp), 2) if rec.adp is not None else None,
                "auction_value": float(rec.auction_value) if rec.auction_value is not None else None,
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

        market_only = [
            MarketOnlyRow(id=pid, name=names[pid][0], espn_id=names[pid][1], position=positions.get(pid))
            for pid in market_only_ids
            if pid in names
        ]
        market_only.sort(key=lambda r: (market[r.id]["overall_rank"] is None,
                                        market[r.id]["overall_rank"] or 0))

        return BoardInputs(
            season=season, pool=list(pool.values()), source=source,
            last_season_gp=last_season_gp, projected_gp=projected_gp,
            projections_as_of=projections_as_of,
            market=market, market_as_of=market_as_of, market_only=market_only,
            positions=positions, names=names,
            session_picked=frozenset(session_picked), session_mine=frozenset(session_mine),
            used_picks=tuple(sorted(used_picks)), keeper_picks=tuple(sorted(keeper_picks)),
        )

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
    def _build_board(
        scoring: "ResolvedScoring",
        picked_ids: frozenset[int],
        my_ids: frozenset[int],
        inputs: BoardInputs,
        session: Optional[BoardSession] = None,
    ) -> DraftBoardResp:
        session = session or BoardSession()
        cat_defs = rankable_categories(scoring) if scoring.is_categories else []

        picked = picked_ids | inputs.session_picked
        mine = my_ids | inputs.session_mine
        removed = picked | mine

        # (row, value, per-category values, per-category z, z-sum), best first.
        if scoring.is_categories:
            scored = compute_category_scores(inputs.pool, cat_defs)
            entries = [(s.row, category_value(s.score), s.values, s.z, s.score) for s in scored]
        else:
            points = scoring.points
            entries = [(row, round(points.score(row.line), VALUE_DECIMALS), None, None, None)
                       for row in inputs.pool]
            entries.sort(key=lambda e: (-e[1], -e[0].fpts_avg))

        primary = DraftBoardService._primary_positions(inputs)
        eligible = DraftBoardService._eligible_slots(inputs)
        limits = DraftBoardService._position_limits(scoring)
        cap_check = DraftBoardService._cap_check(limits, mine, primary, inputs.positions)

        # What this roster is short of and what it has conceded: the weights the
        # fit column is scored with (None for points leagues, which have no
        # per-category z's to weigh).
        fit = DraftBoardService._fit_model(scoring, session, entries, mine, cat_defs)
        fit_values = DraftBoardService._fit_values(fit, entries)
        fit_ranks = DraftBoardService._fit_ranks(fit_values, removed)
        league_size = DraftBoardService._league_size(scoring, session)
        horizon = DraftBoardService._availability_horizon(session)

        rows: list[DraftBoardRow] = []
        candidates: list[dict] = []
        for cv_rank, (row, value, cats, z, z_sum) in enumerate(entries, start=1):
            market = inputs.market.get(row.id, {})
            market_rank = market.get("overall_rank")
            gp = inputs.projected_gp.get(row.id) or DEFAULT_PROJECTED_GP
            blocked = cap_check(row.id)
            if row.id not in removed:
                rows.append(DraftBoardRow(
                    player_id=row.id,
                    espn_id=row.espn_id,
                    name=row.name,
                    team=row.team,
                    position=inputs.positions.get(row.id),
                    primary_position=primary.get(row.id),
                    positions=eligible.get(row.id),
                    injury_status=DraftBoardService._injury_of(market),
                    cv_rank=cv_rank,
                    value=value,
                    value_source=inputs.source.get(row.id, "baseline"),
                    last_season_gp=inputs.last_season_gp.get(row.id),
                    projected_gp=inputs.projected_gp.get(row.id),
                    fpts_avg=row.fpts_avg,
                    market_rank=market_rank,
                    adp=market.get("adp"),
                    auction_value=market.get("auction_value"),
                    market_delta=(market_rank - cv_rank) if market_rank is not None else None,
                    fit_value=fit_values.get(row.id),
                    fit_rank=fit_ranks.get(row.id),
                    availability=DraftBoardService._availability_of(market, horizon, league_size),
                    cap_blocked=blocked,
                    categories=cats,
                    category_z=z,
                    score=z_sum,
                ))
            candidates.append({
                "id": row.id, "name": row.name, "value": value, "team": row.team,
                "source": inputs.source.get(row.id, "baseline"),
                "season_value": round(value * gp, VALUE_DECIMALS),
                "gp": gp,
                "z": z,
                "fit_value": fit_values.get(row.id),
                "position": primary.get(row.id),
                "available": row.id not in removed,
                "blocked": blocked,
                "injury": DraftBoardService._injury_of(market),
                "slots": eligible.get(row.id),
            })

        # Market-only rows sit after everything valued: they have no value to
        # rank by, only ESPN's opinion that they are worth drafting.
        for entry in inputs.market_only:
            if entry.id in removed:
                continue
            market = inputs.market.get(entry.id, {})
            market_rank = market.get("overall_rank")
            rows.append(DraftBoardRow(
                player_id=entry.id,
                espn_id=entry.espn_id,
                name=entry.name,
                team=None,
                position=entry.position,
                primary_position=primary.get(entry.id),
                positions=eligible.get(entry.id),
                injury_status=DraftBoardService._injury_of(market),
                cv_rank=None,
                value=None,
                value_source="market",
                last_season_gp=None,
                projected_gp=None,
                fpts_avg=None,
                market_rank=market_rank,
                adp=market.get("adp"),
                auction_value=market.get("auction_value"),
                market_delta=None,
                fit_value=None,
                fit_rank=None,
                availability=DraftBoardService._availability_of(market, horizon, league_size),
                cap_blocked=cap_check(entry.id),
                categories=None,
                category_z=None,
                score=None,
            ))

        recommendations = DraftBoardService._recommend(
            candidates, scoring, session, mine, primary, fit
        )

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
                player_id=entry.id, name=entry.name, team=None,
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
                player_id=pid, name=name, team=None,
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
                session_id=session.session_id,
                league_size=league_size,
                roster_slots=DraftBoardService._roster_slots(scoring),
                position_source=("espn" if primary else ("coarse" if any(inputs.positions.values()) else "none")),
                position_limits=limits,
                categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
                # The keys actually weighing zero, which for a category league is
                # what "punted" means; a points league has none to apply and
                # simply echoes what the session stores.
                punts=(fit.punts if fit is not None else list(session.punts)),
                category_need=DraftBoardService._category_need(fit),
                settings_synced=scoring.settings_synced if scoring.league is not None else None,
                # dd/td weights score 0 against aggregate lines; name them rather
                # than imply the league's weights were fully applied (the
                # RankingsService._league_scoring rule).
                unsupported=([k for k in GAME_ONLY_KEYS if k in scoring.points.weights]
                             if not scoring.is_categories else []),
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
        slots = getattr(scoring.league, "roster_slots", None) if scoring.league is not None else None
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
    def _injury_of(market: Mapping) -> Optional[str]:
        """The market row's injury status, with "healthy" reported as nothing."""
        status = market.get("injury_status")
        if not status or str(status).upper() in ("ACTIVE", "NORMAL"):
            return None
        return str(status)

    # ---- category fit ----------------------------------------------------------

    @staticmethod
    def _fit_model(
        scoring: "ResolvedScoring",
        session: BoardSession,
        entries: list,
        my_ids: frozenset[int],
        cat_defs: list,
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
        ranked = [(row.id, z) for row, _value, _cats, z, _z_sum in entries]
        return build_fit_model(ranked, my_ids, cat_defs, tier_size, session.punts)

    @staticmethod
    def _fit_values(fit: Optional[FitModel], entries: list) -> dict[int, float]:
        """Player id -> roster-specific value, on the same scale as `value`."""
        if fit is None:
            return {}
        return {
            row.id: category_value(fit.fit_z(z_sum, z))
            for row, _value, _cats, z, z_sum in entries
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
        market: Mapping, horizon: Optional[int], league_size: Optional[int]
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
            expected = market.get("overall_rank")
        if expected is None:
            return None

        threshold = max(AVAILABILITY_MIN_THRESHOLD, round(league_size / 2))
        gap = float(expected) - horizon
        if gap >= threshold:
            return "likely"
        if gap <= -threshold:
            return "gone"
        return "tossup"

    # ---- recommendations -------------------------------------------------------

    @staticmethod
    def _starters_per_position(roster_slots: Mapping[str, int]) -> dict[str, float]:
        """How many starters a team fields at each of the five ESPN positions.

        A league's derived slots are shared out among the positions that can
        fill them — a `G` slot is half a PG and half an SG, `UT` a fifth of each
        — because a player is counted at exactly one position below, so the
        starters have to be spread the same way.
        """
        starters: dict[str, float] = {p: 0.0 for p in ESPN_POSITIONS}
        for slot, count in (roster_slots or {}).items():
            name = str(slot).strip()
            if name in _NON_STARTING_SLOTS:
                continue
            members = _SLOT_MEMBERS.get(name)
            if not members:
                continue
            try:
                n = float(count)
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            share = n / len(members)
            for position in members:
                starters[position] += share
        return starters

    @staticmethod
    def _startable_tier_left(
        candidates: list[dict], startable: Mapping[str, int]
    ) -> dict[str, int]:
        """How many of each position's startable tier are still undrafted.

        The tier is the top `startable[position]` players at that position over
        the whole pool, drafted or not — a fixed set, so "4 startable centres
        left of 16" counts down through the draft instead of standing still.
        """
        ranked: dict[str, list[dict]] = {p: [] for p in ESPN_POSITIONS}
        for c in candidates:
            if c["position"] in ranked:
                ranked[c["position"]].append(c)
        return {
            position: DraftBoardService._tier_left(entries, startable.get(position, 0))
            for position, entries in ranked.items()
        }

    @staticmethod
    def _tier_left(candidates: list[dict], size: int) -> int:
        """How many of the top `size` candidates by season value are undrafted."""
        if size <= 0:
            return 0
        top = sorted(candidates, key=lambda c: -c["season_value"])[:size]
        return sum(1 for c in top if c["available"])

    @staticmethod
    def _recommend(
        candidates: list[dict],
        scoring: "ResolvedScoring",
        session: BoardSession,
        my_ids: frozenset[int],
        primary: Mapping[int, str],
        fit: Optional[FitModel] = None,
    ) -> list[DraftRecommendation]:
        """Rank what is left by VORP with the visible adjustments applied."""
        pool = [c for c in candidates if c["available"] and not c["blocked"]]
        if not pool:
            return []

        roster_slots = DraftBoardService._roster_slots(scoring)
        league_size = DraftBoardService._league_size(scoring, session) or 0
        starters = DraftBoardService._starters_per_position(roster_slots)

        # The startable tier is fixed against the *full* pool — the top
        # `league_size x starters` players at each position, drafted or not —
        # and what moves is how many of it survive. A tier recomputed over
        # survivors refills itself from below after every pick and never runs
        # dry, which is the whole thing worth measuring.
        startable: dict[str, int] = {
            p: int(round(league_size * starters.get(p, 0.0))) for p in ESPN_POSITIONS
        }
        tier_left = DraftBoardService._startable_tier_left(candidates, startable)

        # Replacement level is the *marginal* starter still to be filled: index
        # the surviving tier count into the available players at that position.
        # Indexing the original need instead would slide deeper into the
        # distribution as the top came off the board and make the bar FALL
        # through the draft — handing a position that had just been picked
        # clean a growing VORP premium, which the scarcity term would then
        # double down on.
        by_position: dict[str, list[float]] = {p: [] for p in ESPN_POSITIONS}
        for c in pool:
            if c["position"] in by_position:
                by_position[c["position"]].append(c["season_value"])
        for values in by_position.values():
            values.sort(reverse=True)

        replacement: dict[str, float] = {}
        for position, values in by_position.items():
            if not values:
                replacement[position] = 0.0
                continue
            # Tier exhausted: every team that starts one has one, so the next
            # player at this position is worth what the best remaining one is.
            remaining = tier_left.get(position, 0) if startable[position] > 0 else len(values) - 1
            replacement[position] = values[min(max(remaining, 0), len(values) - 1)]

        # A player with no known position is measured against the pool at large,
        # by the same remaining-need rule.
        overall = sorted((c["season_value"] for c in pool), reverse=True)
        overall_need = int(round(league_size * sum(starters.values())))
        overall_left = DraftBoardService._tier_left(candidates, overall_need)
        default_replacement = (
            overall[min(overall_left, len(overall) - 1)] if overall and overall_need > 0 else 0.0
        )

        my_counts = Counter(primary[pid] for pid in my_ids if pid in primary)
        league_slots = {
            str(s).strip() for s in (roster_slots or {})
            if str(s).strip() not in _NON_STARTING_SLOTS and str(s).strip() not in _UNIVERSAL_SLOTS
        }

        scored: list[DraftRecommendation] = []
        for c in pool:
            position = c["position"]
            season_value = c["season_value"]
            bar = replacement.get(position, default_replacement) if position else default_replacement
            vorp = round(season_value - bar, VALUE_DECIMALS)
            headroom = max(vorp, 0.0)

            # Scarcity: how far this position's startable tier has been drawn
            # down, damped when my own roster does not need it yet.
            need = startable.get(position, 0) if position else 0
            left = tier_left.get(position, 0) if position else 0
            pressure = max(0.0, 1.0 - (left / need)) if need > 0 else 0.0
            my_need = starters.get(position, 0.0) - my_counts.get(position, 0) if position else 0.0
            need_factor = 1.0 if my_need > 0 else SCARCITY_IDLE_NEED
            scarcity = round(SCARCITY_WEIGHT * pressure * need_factor * headroom, VALUE_DECIMALS)

            # Flexibility: startable lineup slots beyond the first (UT excluded —
            # everyone is UT-eligible, so it is not an advantage).
            slots = [s for s in (c["slots"] or []) if s in league_slots]
            extra_slots = max(0, len(set(slots)) - 1)
            flexibility = round(FLEX_RATE * extra_slots * max(season_value, 0.0), VALUE_DECIMALS)

            penalty = INJURY_PENALTY.get(str(c["injury"]).upper(), 0.0) if c["injury"] else 0.0
            injury = -round(penalty * max(season_value, 0.0), VALUE_DECIMALS)

            # What this roster gains (or gives up) beyond the balanced value:
            # the same season scale, so the sum stays interpretable. Zero for
            # points leagues and for anyone the pool cannot score.
            fit_value = c.get("fit_value")
            category_fit = (
                round((fit_value - c["value"]) * c["gp"], VALUE_DECIMALS)
                if fit_value is not None else 0.0
            )

            score = round(
                vorp + scarcity + flexibility + injury + category_fit, VALUE_DECIMALS
            )
            scored.append(DraftRecommendation(
                player_id=c["id"],
                name=c["name"],
                primary_position=position,
                value=c["value"],
                season_value=season_value,
                vorp=vorp,
                score=score,
                components=[
                    RecommendationComponent(
                        key="season_value", label="Season value", value=season_value, in_score=False,
                        detail=f"{c['value']} per game over a projected season",
                    ),
                    RecommendationComponent(
                        key="vorp", label="Value over replacement", value=vorp, in_score=True,
                        detail=(
                            f"replacement at {position} is {round(bar, VALUE_DECIMALS)}" if position
                            else f"no position data — measured against the pool ({round(bar, VALUE_DECIMALS)})"
                        ),
                    ),
                    RecommendationComponent(
                        key="scarcity", label="Positional scarcity", value=scarcity, in_score=True,
                        detail=(
                            f"{left} startable {position} left of {need}"
                            + ("" if my_need > 0 else "; your roster is already set there")
                            if position and need > 0 else "no positional pressure"
                        ),
                    ),
                    RecommendationComponent(
                        key="flexibility", label="Lineup flexibility", value=flexibility, in_score=True,
                        detail=(
                            f"starts at {', '.join(sorted(set(slots)))}" if extra_slots
                            else "one starting slot"
                        ),
                    ),
                    RecommendationComponent(
                        key="injury", label="Injury risk", value=injury, in_score=True,
                        detail=(f"listed {c['injury']}" if c["injury"] else "no injury flag"),
                    ),
                    RecommendationComponent(
                        key="category_fit", label="Category fit", value=category_fit, in_score=True,
                        detail=DraftBoardService._fit_detail(fit, c.get("z"), c["gp"]),
                    ),
                ],
                reason=DraftBoardService._reason(
                    c["name"], position, vorp, scarcity, flexibility, injury, category_fit,
                    c["injury"],
                ),
            ))

        scored.sort(key=lambda r: (-r.score, -r.season_value))
        return scored[:RECOMMENDATION_COUNT]

    @staticmethod
    def _fit_detail(
        fit: Optional[FitModel], z: Optional[Mapping[str, float]], gp: float
    ) -> str:
        """Which categories moved this candidate off his balanced value.

        The two named are the largest movers, in the same season-value points
        as the component. They can fall a little short of summing to it: the
        value scale is clamped at zero, so for a player below the floor part of
        the shift has nowhere to land.
        """
        if fit is None:
            return "points league — value is already this league's own scoring"
        drivers = fit.drivers(z)[:2]
        if not drivers:
            return "balanced: no category pulls this pick either way"
        parts = []
        for need, shift in drivers:
            amount = shift * CATEGORY_VALUE_SCALE * gp
            if need.punted:
                why = "punted"
            else:
                why = f"{abs(need.need):.1f}σ {'behind' if need.need > 0 else 'ahead of'} pace"
            parts.append(f"{amount:+.1f} {need.label} ({why})")
        return ", ".join(parts)

    @staticmethod
    def _reason(
        name: str,
        position: Optional[str],
        vorp: float,
        scarcity: float,
        flexibility: float,
        injury: float,
        category_fit: float,
        injury_status: Optional[str],
    ) -> str:
        """One sentence naming the terms that actually moved the score."""
        where = f" at {position}" if position else ""
        parts = [f"{vorp:+.1f} over replacement{where}"]
        if scarcity:
            parts.append(f"{scarcity:+.1f} for scarcity")
        if flexibility:
            parts.append(f"{flexibility:+.1f} for lineup flexibility")
        if category_fit:
            parts.append(f"{category_fit:+.1f} for category fit")
        if injury:
            parts.append(f"{injury:+.1f} for {injury_status}")
        return f"{name}: " + ", ".join(parts)

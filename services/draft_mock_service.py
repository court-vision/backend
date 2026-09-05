"""Draft Lab mock mode: CV plays the other seats so a room can be driven end to
end without anyone else in it.

The point is not realism. It is that a full draft — category fit, availability,
hard caps and all — can be played in a dozen keystrokes, so the recommendation
engine is exercised a hundred times before anyone's real draft.

**Each seat takes the best available by `adp ?? market_rank`**, with a small
preference for reaching a few names down the board, and never past a hard
position cap. That is the whole model. There is deliberately no roster-need
modelling: league-mate modelling is a v2 question, and an autopicker that
"knows" needs would be a worse copy of it while reading as though the mock's
standings meant something. The seats are a clock, not opponents.

Three properties are worth stating outright, because the tests are built on
them and the room's copy promises them:

1. **Deterministic per pick.** The RNG is seeded from `(session_id,
   overall_pick)` — one RNG per *pick*, never per advance. So advancing a room
   turn by turn produces exactly the draft advancing it in one call would, and
   a mock replays identically. Two different rooms deliberately draft
   differently: the session id is in the seed, which is what makes "run another
   mock" useful.
2. **A cap stops the run; it is never breached.** If every remaining player
   would break the seat-on-the-clock's caps, the advance stops and says so.
   Silently relaxing a cap would make the room's own `CAP` badges a lie.
3. **The pool is the market snapshot** (~450 ranked players in production) —
   which is also where primary positions come from, so the exact ESPN cap rule
   applies. Only when the season has no market snapshot at all does the
   autopicker fall back to CV value (`cv_rank`), which costs a board-sized
   fetch; the response flags it, because "drafted by ADP" and "drafted by our
   own ranking" are not the same claim.

Everything above the `DraftMockService` class is pure — no database, no ORM, no
I/O — so the simulation is unit-testable without Postgres. The service is the
one place that reads and writes, and it writes `DraftPick` rows directly:
`DraftPickCreate.source` excludes `mock` on purpose (only the server may assert
the autopicker made a pick), so `DraftService.add_pick` is not a route in.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Mapping, Optional

from peewee import IntegrityError

from core.errors import BadRequestError, ConflictError
from core.settings import settings
from db.base import db, db_operation
from db.models.drafts import DraftPick, DraftSession
from db.models.nba.draft_market import DraftMarket
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.draft import MockAdvanceRequest, MockAdvanceResp, MockAdvanceResponse
from services.draft_board_service import DraftBoardService
from services.draft_service import (
    DraftService,
    _session_resp,
    draft_front,
    next_pick_for_slot,
    pick_placement,
    slot_of,
    total_picks_of,
)
from services.scoring.category_value import rankable_categories
from services.scoring.providers.espn_settings import POSITION_ID_MAP
from services.scoring.resolver import resolve_scoring

# Reseeding namespace. Bumping it reseeds every mock in the world, on purpose:
# it is the one switch that says "the autopicker changed, old mocks will not
# replay". The digest is explicit rather than handing a string to
# `random.Random` — the determinism promise must not rest on CPython's internal
# seeding, and `hash()` is out entirely (PYTHONHASHSEED randomizes it per
# process, so the same room would replay differently after a restart).
SEED_NAMESPACE = "court-vision/draft-mock/v1"

# How far past the best available a seat will reach, and how sharply the
# preference decays. 0.6^k over five offsets puts ~43% on the top of the board
# and ~6% on the fifth name: enough that two mocks differ, not so much that the
# board's ordering stops meaning anything. Reaching is what real drafters do —
# it is deliberately not a needs model.
REACH_DECAY = 0.6
REACH_DEPTH = 5
REACH_WEIGHTS = tuple(REACH_DECAY ** k for k in range(REACH_DEPTH))


# ---- pure simulation -------------------------------------------------------


@dataclass(frozen=True)
class MockCandidate:
    """One draftable player, as the autopicker sees him: an identity and a place
    in the queue. Nothing about value, position or need — the cap rule is
    injected, and there is no need model."""

    player_id: int
    espn_id: Optional[int] = None
    name: Optional[str] = None
    order_key: float = 0.0      # adp ?? market_rank ?? cv_rank; lower drafts sooner


@dataclass(frozen=True)
class MockGeometry:
    """Where the draft is and what shape it has. Every field is derived by
    `draft_service`, whose arithmetic the replay harness guards — this module
    re-derives none of it."""

    session_id: int
    league_size: int
    draft_type: str
    total_picks: int
    front: int                          # one past the last pick made on the clock
    my_slot: Optional[int] = None
    used: frozenset[int] = frozenset()          # pick numbers already recorded
    keeper_picks: frozenset[int] = frozenset()  # the subset spent before the draft started


@dataclass(frozen=True)
class SimPick:
    """One pick the autopicker decided on, before anything is written."""

    overall_pick: int
    round: Optional[int]
    slot: Optional[int]
    candidate: MockCandidate
    by_me: bool


@dataclass(frozen=True)
class MockResult:
    picks: tuple[SimPick, ...]
    stopped_at: Optional[int]           # first pick not made; None when the draft ran out
    stopped_reason: str
    blocked_slot: Optional[int] = None  # the seat a cap stop was for


def mock_rng(session_id: int, overall_pick: int) -> random.Random:
    """The RNG for one pick — a pure function of the room and the pick number."""
    digest = hashlib.blake2b(
        f"{SEED_NAMESPACE}:{session_id}:{overall_pick}".encode(), digest_size=8
    ).digest()
    return random.Random(int.from_bytes(digest, "big"))


def reach_index(rng: random.Random, count: int) -> int:
    """Which of the top `count` eligible candidates this seat takes.

    Exactly one draw per pick whatever `count` is, so a seed maps to an outcome
    the same way at the head of the board as at its tail. The weights
    renormalize when fewer than `REACH_DEPTH` candidates remain, so the last
    picks of a draft are still a choice rather than a forced take.
    """
    if count <= 1:
        return 0
    weights = REACH_WEIGHTS[:count]
    target = rng.random() * sum(weights)
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if target < cumulative:
            return index
    return len(weights) - 1


def order_key_of(adp: Optional[float], market_rank: Optional[int]) -> Optional[float]:
    """Where the market expects a player to go: ADP, else the editorial rank.

    The same preference order the board's availability buckets use — ADP is what
    real drafts did, the editorial rank is what ESPN says they should have done.
    None when the snapshot carries neither, and the caller drops the candidate:
    a player nothing ranks is not one the other seats would reach for.
    """
    if adp is not None:
        return float(adp)
    if market_rank is not None:
        return float(market_rank)
    return None


def sort_candidates(candidates: Iterable[MockCandidate]) -> list[MockCandidate]:
    """Draft order, with `player_id` breaking ties.

    Explicit rather than incidental: leaving equal-ranked players in whatever
    order Postgres returned them would make a mock replay differently after a
    vacuum, which is exactly the promise this module makes.
    """
    return sorted(candidates, key=lambda c: (c.order_key, c.player_id))


def simulate(
    geometry: MockGeometry,
    candidates: list[MockCandidate],
    seat_rosters: Mapping[int, frozenset[int]],
    cap_check_for: Callable[[frozenset[int]], Callable[[int], bool]],
    until: str = "my_turn",
) -> MockResult:
    """Play the draft forward from the front. Pure: decides, never writes.

    `candidates` must already be in draft order (`sort_candidates`) and must
    exclude everyone already drafted. `cap_check_for` builds the cap predicate
    for one roster — production passes `DraftBoardService._cap_check` bound to
    the league's limits, so the autopicker enforces the same rule the board
    greys players out with rather than a second opinion about it.

    `until="my_turn"` stops before my next turn; with no turn of mine left it
    runs to the end, which is how a mock finishes on that button alone.
    """
    limit = geometry.total_picks
    if until == "my_turn":
        my_next = next_pick_for_slot(
            geometry.front, geometry.my_slot, geometry.league_size, geometry.draft_type,
            skip=geometry.keeper_picks, last=geometry.total_picks,
        )
        if my_next is not None:
            limit = my_next - 1

    rosters: dict[int, set[int]] = {
        slot: set(ids) for slot, ids in seat_rosters.items()
    }
    remaining = list(candidates)
    taken: set[int] = set()
    picks: list[SimPick] = []

    overall = geometry.front
    while overall <= limit:
        # A keeper, or a pick a live sync placed ahead of the front: spent
        # already, and not a turn for anyone.
        if overall in geometry.used:
            overall += 1
            continue

        seat = slot_of(overall, geometry.league_size, geometry.draft_type)
        roster = frozenset(rosters.get(seat, set())) if seat is not None else frozenset()
        blocked = cap_check_for(roster)

        # The window is the top REACH_DEPTH *eligible* names, so a reach never
        # lands on a player this seat cannot draft.
        eligible: list[int] = []
        any_left = False
        for index, candidate in enumerate(remaining):
            if candidate.player_id in taken:
                continue
            any_left = True
            if blocked(candidate.player_id):
                continue
            eligible.append(index)
            if len(eligible) == REACH_DEPTH:
                break

        if not eligible:
            return MockResult(
                picks=tuple(picks),
                stopped_at=overall,
                stopped_reason="cap_blocked" if any_left else "pool_exhausted",
                blocked_slot=seat if any_left else None,
            )

        chosen = remaining[eligible[reach_index(mock_rng(geometry.session_id, overall), len(eligible))]]
        pick_round, pick_slot = pick_placement(
            overall, geometry.league_size, geometry.draft_type
        )
        picks.append(SimPick(
            overall_pick=overall,
            round=pick_round,
            slot=pick_slot,
            candidate=chosen,
            by_me=pick_slot is not None and pick_slot == geometry.my_slot,
        ))
        taken.add(chosen.player_id)
        if seat is not None:
            rosters.setdefault(seat, set()).add(chosen.player_id)
        # Drop what has been taken off the head so the scan stays short; a
        # player capped for this seat stays in the queue for the next one.
        while remaining and remaining[0].player_id in taken:
            remaining.pop(0)
        overall += 1

    ran_out = limit >= geometry.total_picks
    return MockResult(
        picks=tuple(picks),
        stopped_at=None if ran_out else limit + 1,
        stopped_reason="end" if ran_out else "my_turn",
    )


# ---- the service -----------------------------------------------------------


@dataclass
class MockPool:
    """The candidates and everything the cap rule needs to judge them."""

    candidates: list[MockCandidate]
    primary: dict[int, str]                     # ESPN primary position (caps count on this)
    coarse: dict[int, Optional[str]]            # nba_api G/F/C, the cap fallback
    market_as_of: Optional[date] = None
    fallback: bool = False


class DraftMockService:
    """The autopicker. One entry point, one transaction."""

    @staticmethod
    @db_operation("drafts.mock_advance")
    def advance(session_id: int, req: MockAdvanceRequest) -> MockAdvanceResponse:
        session = DraftService._session_or_404(session_id)
        DraftMockService._check_simulatable(session, req.until)

        scoring = resolve_scoring(session.league if session.league_id is not None else None)
        limits = DraftBoardService._position_limits(scoring)
        league_size = len(session.pick_order or [])
        total_picks = total_picks_of(session)

        # The pool is fetched outside the transaction — it is the expensive read
        # and nothing in it is per-room. The picks are then re-read inside, so
        # the geometry and the candidate list come from one snapshot; a pick
        # that lands in between is caught by the unique indexes either way.
        pool = DraftMockService._pool(
            session_id, scoring, DraftMockService._drafted(session_id)
        )

        def cap_check_for(roster: frozenset[int]):
            return DraftBoardService._cap_check(limits, roster, pool.primary, pool.coarse)

        with db.atomic():
            picks = DraftService._picks_of(session_id)
            drafted = {p.player_id for p in picks if p.player_id is not None}
            candidates = [c for c in pool.candidates if c.player_id not in drafted]
            geometry = MockGeometry(
                session_id=session_id,
                league_size=league_size,
                draft_type=session.draft_type,
                total_picks=total_picks,
                front=draft_front(
                    [p.overall_pick for p in picks],
                    [p.overall_pick for p in picks if p.source == "keeper"],
                ),
                my_slot=session.my_slot,
                used=frozenset(p.overall_pick for p in picks),
                keeper_picks=frozenset(p.overall_pick for p in picks if p.source == "keeper"),
            )
            result = simulate(
                geometry,
                candidates,
                DraftMockService._seat_rosters(picks),
                cap_check_for,
                until=req.until,
            )
            # Nothing recorded is nothing changed: the room was active on the
            # way in (the guard says so), so it is still active on the way out.
            completed = False
            if result.picks:
                DraftMockService._record(session_id, result.picks)
                completed = DraftMockService._stamp(session, len(picks) + len(result.picks))

        stored = DraftService._picks_of(session_id)
        data = MockAdvanceResp(
            session=_session_resp(
                session,
                used_picks=[p.overall_pick for p in stored],
                picks=stored,
                keeper_count=DraftService._keeper_count_of(session),
                keeper_picks=[p.overall_pick for p in stored if p.source == "keeper"],
            ),
            picks_made=len(result.picks),
            until=req.until,
            from_pick=geometry.front,
            stopped_at=result.stopped_at,
            stopped_reason=result.stopped_reason,
            completed=completed,
            fallback=pool.fallback,
            market_as_of=pool.market_as_of,
        )
        return MockAdvanceResponse(
            status=ApiStatus.SUCCESS,
            message=DraftMockService._message(result, pool.fallback),
            data=data,
        )

    # ---- guards ------------------------------------------------------------

    @staticmethod
    def _check_simulatable(session: DraftSession, until: str) -> None:
        """Which rooms the autopicker may touch, and what it needs to run.

        A manual or live room is tracking a real draft, and a mock room linked
        to an ESPN lobby is taking its picks from that lobby: simulating over
        either writes fiction the next sync would collide with on every pick
        number.
        """
        if session.kind != "mock":
            raise ConflictError(
                "NOT_A_MOCK",
                f"Draft room #{session.id} is a {session.kind} room, tracking a real draft"
                " — only a mock room can be simulated",
            )
        if session.espn_league_id is not None:
            raise ConflictError(
                "MOCK_ROOM_IS_LINKED",
                f"This room is following ESPN draft {session.espn_league_id}; its picks come from"
                " there. Open an unlinked mock room to simulate one.",
                data={"espn_league_id": session.espn_league_id},
            )
        if session.status != "active":
            raise ConflictError(
                "DRAFT_NOT_ACTIVE", f"This draft is {session.status}; there is nothing to simulate"
            )
        if session.draft_type != "snake":
            raise BadRequestError(
                "DRAFT_MOCK_SNAKE_ONLY",
                "The autopicker drafts by draft order and has no bidding model; auction rooms are entered by hand",
            )
        if total_picks_of(session) is None:
            raise BadRequestError(
                "DRAFT_MOCK_NEEDS_SHAPE",
                "This room has no pick order or no round count, so it has no seats to play and no"
                " end to run to — set them first",
            )
        # `end` without a slot is fine: nothing is mine, and watching a whole
        # draft happen is a legitimate thing to ask for.
        if until == "my_turn" and session.my_slot is None:
            raise BadRequestError(
                "DRAFT_MOCK_NEEDS_SLOT",
                "This room has no slot confirmed, so there is no turn of yours to stop at"
                " — set `my_slot`, or simulate to the end",
            )

    # ---- inputs ------------------------------------------------------------

    @staticmethod
    def _drafted(session_id: int) -> set[int]:
        """Who is already off the board, for the pool query's sake only — the
        transaction re-reads the picks and filters again on what it finds."""
        return {p.player_id for p in DraftService._picks_of(session_id) if p.player_id is not None}

    @staticmethod
    def _seat_rosters(picks: Iterable[DraftPick]) -> dict[int, frozenset[int]]:
        """Seat -> the players it already holds.

        `slot` is the seat ESPN attributed the pick to where it knows one (a
        traded pick lands where it was actually made), else the snake geometry's
        — the attribution the room model made trustworthy. A pick whose player
        has not resolved yet holds no id and so counts against nobody's caps;
        `_picks_of` has already resolved everyone it can.
        """
        rosters: dict[int, set[int]] = {}
        for pick in picks:
            if pick.slot is None or pick.player_id is None:
                continue
            rosters.setdefault(int(pick.slot), set()).add(pick.player_id)
        return {slot: frozenset(ids) for slot, ids in rosters.items()}

    @staticmethod
    def _pool(session_id: int, scoring, drafted: frozenset[int] | set[int]) -> MockPool:
        """Who is left to draft, in the order the other seats would take them.

        The market snapshot is both the queue and the source of the primary
        positions caps count on, so the ordinary path is two indexed queries.
        With no snapshot for the season there is no ADP to draft by and no
        cheaper honest answer than CV value, so the fallback pays for a
        board-sized fetch — the dev database's only path, and flagged in the
        response so nobody reads it as ADP.
        """
        season = settings.nba_season
        market = {rec.player_id: rec for rec in DraftMarket.latest_for_season(season)}
        if market:
            return DraftMockService._market_pool(market, drafted)
        return DraftMockService._value_pool(session_id, scoring, drafted)

    @staticmethod
    def _market_pool(market: Mapping[int, object], drafted) -> MockPool:
        wanted = set(market) | set(drafted)
        players = {
            rec.id: rec
            for rec in Player.select(Player.id, Player.name, Player.espn_id, Player.position)
            .where(Player.id.in_(list(wanted)))
        }
        primary: dict[int, str] = {}
        coarse: dict[int, Optional[str]] = {}
        candidates: list[MockCandidate] = []
        market_as_of: Optional[date] = None
        for pid, rec in market.items():
            market_as_of = rec.as_of_date
            name = POSITION_ID_MAP.get(rec.default_position_id or 0)
            if name:
                primary[pid] = name
            player = players.get(pid)
            if player is not None:
                coarse[pid] = player.position
            if pid in drafted:
                continue
            key = order_key_of(
                float(rec.adp) if rec.adp is not None else None,
                int(rec.overall_rank) if rec.overall_rank is not None else None,
            )
            if key is None or player is None:
                continue
            candidates.append(MockCandidate(
                player_id=pid, espn_id=player.espn_id, name=player.name, order_key=key
            ))
        # The positions of players already on a seat, which the exact cap rule
        # needs to count them: a roster player the snapshot does not carry sends
        # that seat to the coarse fallback.
        for pid in drafted:
            player = players.get(pid)
            if player is not None:
                coarse.setdefault(pid, player.position)
        return MockPool(
            candidates=sort_candidates(candidates),
            primary=primary,
            coarse=coarse,
            market_as_of=market_as_of,
            fallback=False,
        )

    @staticmethod
    def _value_pool(session_id: int, scoring, drafted) -> MockPool:
        """CV value as the draft order, when there is no market to draft by."""
        inputs = DraftBoardService._fetch_inputs(frozenset(), session_id)
        cat_defs = rankable_categories(scoring) if scoring.is_categories else []
        entries = DraftBoardService.rank_pool(scoring, inputs.pool, cat_defs)
        candidates = [
            MockCandidate(
                player_id=row.id,
                espn_id=row.espn_id,
                name=row.name,
                order_key=float(cv_rank),
            )
            for cv_rank, (row, *_rest) in enumerate(entries, start=1)
            if row.id not in drafted
        ]
        return MockPool(
            candidates=candidates,       # already in cv_rank order; keys are unique
            primary=DraftBoardService._primary_positions(inputs),
            coarse=dict(inputs.positions),
            market_as_of=None,
            fallback=True,
        )

    # ---- writing -----------------------------------------------------------

    @staticmethod
    def _record(session_id: int, picks: tuple[SimPick, ...]) -> None:
        """Every simulated pick, or none of them.

        One statement, all-or-nothing — unlike the INIT reconciliation, which
        savepoints each pick because one bad row there must not lose the other
        forty-four. An advance is a single action, and half a simulated draft is
        worse than none. `espn_team_id` stays null: the autopicker is not ESPN
        saying anything, and `slot` already carries the seat.
        """
        now = datetime.utcnow()
        rows = [
            {
                "session": session_id,
                "overall_pick": pick.overall_pick,
                "round": pick.round,
                "slot": pick.slot,
                "player": pick.candidate.player_id,
                "espn_player_id": pick.candidate.espn_id,
                "espn_team_id": None,
                "player_name": pick.candidate.name,
                "by_me": pick.by_me,
                "source": "mock",
                "bid": None,
                "created_at": now,
            }
            for pick in picks
        ]
        try:
            DraftPick.insert_many(rows).execute()
        except IntegrityError as exc:
            first = picks[0]
            raise DraftService._pick_conflict(
                exc, first.overall_pick, first.candidate.name
            ) from exc

    @staticmethod
    def _stamp(session: DraftSession, pick_count: int) -> bool:
        """The same bookkeeping a hand-entered pick does: the first pick starts
        the draft, and the last one closes the room — for every kind, because
        the recap needs a finished signal that a mock would otherwise never
        send."""
        fields = [DraftSession.updated_at]
        if session.started_at is None:
            session.started_at = datetime.utcnow()
            fields.append(DraftSession.started_at)
        closes = DraftService._closes_draft(session, pick_count)
        if closes:
            fields += DraftService._complete(session)
        session.updated_at = datetime.utcnow()
        session.save(only=fields)
        return closes

    # ---- copy --------------------------------------------------------------

    @staticmethod
    def _message(result: MockResult, fallback: bool) -> str:
        made = len(result.picks)
        picks = f"{made} pick{'s' if made != 1 else ''}"
        if result.stopped_reason == "end":
            body = f"Simulated {picks} — the draft is complete"
        elif result.stopped_reason == "my_turn":
            body = (
                f"You are already on the clock at pick {result.stopped_at}" if made == 0
                else f"Simulated {picks} — you are on the clock at pick {result.stopped_at}"
            )
        elif result.stopped_reason == "cap_blocked":
            body = (
                f"Simulated {picks} — stopped at pick {result.stopped_at}: every remaining player"
                f" would break seat {result.blocked_slot}'s position caps"
            )
        else:
            body = (
                f"Simulated {picks} — stopped at pick {result.stopped_at}:"
                " nothing draftable is left on the board"
            )
        if fallback:
            body += f" (seats drafted by CV value — no market snapshot for {settings.nba_season})"
        return body

"""The Draft Recap endpoint: a finished room, read back.

One board read and one pass over the session's own picks. The board is where
every number comes from — `DraftBoardService.rank_pool` is the ladder that
prices a pick, ranks it, and (in a category league) supplies the per-player z
that the seats' projected standings are summed from — so the recap and the room
can never disagree about what a player was worth.

The database work and the arithmetic are split the way `get_board` splits them:
z-scoring the pool is CPU, and it must not hold a database permit
(`core/compute.py`). The math itself lives in `services/draft_recap.py`, pure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from core.compute import run_cpu
from db import base as db_base
from schemas.common import ApiStatus, CategoryDefResp
from schemas.draft import (
    DraftRecapResp,
    RecapCategoryLine,
    RecapH2HCell,
    RecapMeta,
    RecapPickResp,
    RecapSeatResp,
    RecapStandingResp,
)
from services.draft_board_service import (
    DEFAULT_PROJECTED_GP,
    VALUE_DECIMALS,
    BoardInputs,
    DraftBoardService,
)
from services.draft_recap import Recap, RecapPick, build_recap
from services.draft_service import DraftService
from services.player_value_service import PlayerValueService
from services.scoring.category_value import rankable_categories

if TYPE_CHECKING:  # pragma: no cover
    from api.deps import OwnedDraftSessionContext
    from services.scoring.resolver import ResolvedScoring


class DraftRecapService:

    @staticmethod
    async def get_recap(scoring: "ResolvedScoring", session: "OwnedDraftSessionContext") -> DraftRecapResp:
        """Every pick priced, every seat graded, the standings projected.

        Deliberately not gated on `status`: the arithmetic is defined for a room
        that stopped halfway, and refusing one would only add a way to fail. The
        response says how far the draft got (`complete`, `picks_made`) and the
        page caveats itself.
        """
        inputs, picks = await db_base.run_db(
            "draft_recap.fetch", DraftRecapService._fetch_inputs, session.session_id
        )
        return await run_cpu(
            "draft_recap.build", DraftRecapService._build, scoring, session, inputs, picks
        )

    @staticmethod
    def _fetch_inputs(session_id: int) -> tuple[BoardInputs, list[RecapPick]]:
        """The board's own inputs, plus the picks in full.

        `_fetch_inputs` narrows its pick select to what the board needs and
        keeps none of the row, so the recap reads them again through
        `_picks_of` — one indexed query over at most a few hundred rows, against
        leaving a hot path carrying columns for a second caller. Nothing from
        the ORM crosses into the CPU step.
        """
        inputs = DraftBoardService._fetch_inputs(frozenset(), session_id)
        picks = [
            RecapPick(
                overall_pick=int(pick.overall_pick),
                round=int(pick.round) if pick.round is not None else None,
                slot=int(pick.slot) if pick.slot is not None else None,
                by_me=bool(pick.by_me),
                source=pick.source,
                player_id=pick.player_id,
                espn_player_id=pick.espn_player_id,
                espn_team_id=int(pick.espn_team_id) if pick.espn_team_id is not None else None,
                player_name=pick.player_name,
                bid=float(pick.bid) if pick.bid is not None else None,
            )
            for pick in DraftService._picks_of(session_id)
        ]
        return inputs, picks

    @staticmethod
    def _build(
        scoring: "ResolvedScoring",
        session: "OwnedDraftSessionContext",
        inputs: BoardInputs,
        picks: list[RecapPick],
    ) -> DraftRecapResp:
        cat_defs = rankable_categories(scoring) if scoring.is_categories else []
        entries = DraftBoardService.rank_pool(scoring, inputs.pool, cat_defs)

        ladder = [(row.id, value) for row, value, _cats, _z, _sum in entries]
        category_z = {row.id: z for row, _v, _c, z, _s in entries if z}
        season_value = {
            row.id: round(value * (inputs.projected_gp.get(row.id) or DEFAULT_PROJECTED_GP), VALUE_DECIMALS)
            for row, value, _c, _z, _s in entries
        }
        pool = {row.id: row for row, *_rest in entries}

        recap = build_recap(
            picks,
            ladder,
            inputs.market,
            is_categories=scoring.is_categories,
            categories=cat_defs,
            category_z=category_z,
            season_value=season_value,
            draft_type=session.draft_type,
            my_slot=session.my_slot,
        )
        return DraftRecapService._respond(scoring, session, inputs, recap, pool, cat_defs)

    # ---- mapping onto the wire ------------------------------------------------

    @staticmethod
    def _respond(
        scoring: "ResolvedScoring",
        session: "OwnedDraftSessionContext",
        inputs: BoardInputs,
        recap: Recap,
        pool: dict,
        cat_defs: list,
    ) -> DraftRecapResp:
        rows = []
        for entry in recap.picks:
            pick = entry.pick
            row = pool.get(pick.player_id) if pick.player_id is not None else None
            rows.append(RecapPickResp(
                overall_pick=pick.overall_pick,
                round=pick.round,
                slot=pick.slot,
                by_me=pick.by_me,
                source=pick.source,
                player_id=pick.player_id,
                espn_player_id=pick.espn_player_id or (row.espn_id if row is not None else None),
                player_name=(row.name if row is not None else None) or pick.player_name,
                team=row.team if row is not None else None,
                value=entry.value,
                cv_rank=entry.cv_rank,
                market_rank=entry.market_rank,
                adp=entry.adp,
                surplus_cv=entry.surplus_cv,
                surplus_market=entry.surplus_market,
                value_over_slot=entry.value_over_slot,
                bid=pick.bid,
            ))

        seats = [
            RecapSeatResp(
                slot=seat.slot, espn_team_id=seat.espn_team_id, is_me=seat.is_me,
                picks=seat.picks, unscored=seat.unscored, total_value=seat.total_value,
                value_over_slot=seat.value_over_slot, grade=seat.grade, position=seat.position,
                best_pick=seat.best_pick, worst_pick=seat.worst_pick,
            )
            for seat in recap.seats
        ]

        standings = [
            RecapStandingResp(
                slot=standing.slot,
                categories=[
                    RecapCategoryLine(key=line.key, label=line.label, z_sum=line.z_sum,
                                      rank=line.rank, roto_points=line.roto_points)
                    for line in standing.categories
                ],
                roto_points=standing.roto_points,
                roto_rank=standing.roto_rank,
                season_value=standing.season_value,
                value_rank=standing.value_rank,
                expected_wins=standing.expected_wins,
                h2h=[RecapH2HCell(opponent_slot=c.opponent_slot, won=c.won, lost=c.lost, tied=c.tied)
                     for c in standing.h2h],
            )
            for standing in recap.standings
        ]

        league_size = session.league_size
        total_picks = league_size * session.rounds if league_size and session.rounds else None
        made = len(recap.picks)
        return DraftRecapResp(
            status=ApiStatus.SUCCESS,
            message=DraftRecapService._message(recap, made),
            data=rows,
            seats=seats,
            standings=standings,
            meta=RecapMeta(
                format=scoring.format,
                value_kind=PlayerValueService.value_kind_for(scoring),
                graded_by=recap.graded_by,
                standings_basis="z_sum" if scoring.is_categories else "season_value",
                session_id=session.session_id,
                status=session.status,
                complete=session.status == "completed" or (total_picks is not None and made >= total_picks),
                picks_made=made,
                total_picks=total_picks,
                unscored=recap.unscored,
                unattributed=recap.unattributed,
                league_size=league_size,
                rounds=session.rounds,
                my_slot=session.my_slot,
                draft_type=session.draft_type,
                categories=[CategoryDefResp(**c.to_json()) for c in cat_defs],
                projections_as_of=inputs.projections_as_of,
                market_as_of=inputs.market_as_of,
            ),
        )

    @staticmethod
    def _message(recap: Recap, made: int) -> str:
        if not made:
            return "No picks recorded yet"
        mine: Optional[str] = next((seat.grade for seat in recap.seats if seat.is_me), None)
        seats = f"{len(recap.seats)} seats" if recap.seats else "no seats"
        return f"{made} picks graded across {seats}" + (f"; you drafted a {mine}" if mine else "")

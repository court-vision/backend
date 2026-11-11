"""
Service-to-service jobs, authenticated with the shared pipeline bearer token
(the same `PIPELINE_API_TOKEN` cron-runner presents to data-platform). No user
identity: the caller names the user and team, and ownership is re-checked here.
"""

from fastapi import APIRouter, Security

from core.compute import run_cpu
from core.pipeline_auth import verify_pipeline_token
from core.responses import respond
from schemas.autopilot import (
    CheckinEventsData, CheckinEventsReq, CheckinEventsResp, CheckinRunData, CheckinRunResp, CheckinStarted,
)
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupEvaluateReq, LineupEvaluationResp, LineupReactReq, LineupReactResp
from schemas.move_outcomes import LedgerRunReq, LedgerRunResp
from schemas.scheduled_pickup import PickupExecuteReq, PickupExecuteResp
from schemas.valuation import (
    StandardRankResp,
    StandardValuationData,
    StandardValuationReq,
    StandardValuationResp,
)
from services.ai import checkins, events
from services.late_scratch_service import LateScratchService
from services.lineup_editor_service import LineupEditorService
from services.move_ledger_service import MoveLedgerService
from services.scheduled_pickup_service import ScheduledPickupService
from services.valuation.engine import playoff_weight_of
from services.valuation.standard import (
    STANDARD_LEAGUE_SIZE,
    STANDARD_ROUNDS,
    ProjectedLine,
    season_calendar,
    standard_playoff_weeks,
    standard_ranks,
)

router = APIRouter(prefix="/jobs", tags=["internal jobs"], dependencies=[Security(verify_pipeline_token)])


@router.post("/lineup/evaluate", response_model=LineupEvaluationResp)
async def evaluate_lineup(req: LineupEvaluateReq):
    """Plan today's fill-only moves for one team; apply them when `apply` is set.

    Called by data-platform's lineup-alerts pipeline for every opted-in team.
    Business outcomes are reported in `data.outcome`, never raised.
    """
    return respond(await LineupEditorService.evaluate(req))


@router.post("/lineup/react", response_model=LineupReactResp)
async def react_to_verdict(req: LineupReactReq):
    """Answer one player's verdict change (out, doubtful or available, before his game)
    on every team that may swap: bench him or start him when a move answers it, and
    nothing else on the board.

    Called by data-platform's availability pipeline once per change. Each team's
    outcome is reported in `data.teams`, never raised.
    """
    return respond(await LateScratchService.react(req))


@router.post("/pickups/execute", response_model=PickupExecuteResp)
async def execute_pickups(req: PickupExecuteReq):
    """Attempt every scheduled pickup that is due, up to `limit` rows.

    Called by data-platform's scheduled-pickups pipeline every minute (its route
    gates on whether anything is due). Each row's outcome is reported in
    `data.results`, never raised: executed, skipped (the player is gone — the
    no-op), failed, expired, or deferred (a lock, a waiver period or a writer
    outage; the row waits for `next_attempt_at`). A write is sent at most once:
    one that got no answer is settled by the next attempt from the board alone
    (executed, or failed `interrupted`). With roster writes switched off nothing
    is attempted and due rows are pushed back ten minutes.
    """
    return respond(await ScheduledPickupService.execute_due(req))


@router.post("/ledger/run", response_model=LedgerRunResp)
async def run_move_ledger(req: LedgerRunReq):
    """Rebuild the move ledger (usr.move_outcomes) of every league with a recent snapshot,
    or of the one named.

    Called by data-platform's move_ledger pipeline each night, after the lineup
    snapshots. Each league's rows are replaced whole in one transaction, so a rerun
    is always safe; a league that fails is reported in `data.leagues`, never raised.
    """
    return respond(await MoveLedgerService.run(req))


@router.post("/checkins/run", response_model=CheckinRunResp)
async def run_checkins():
    """One tick of Dime's check-ins: start what is due on each switched-on team.

    Called by data-platform's dime_checkins pipeline every fifteen minutes (its
    route gates on whether any team is switched on). Each team gets at most one
    check-in a tick, one at a time; a window that closed before its check-in
    could run is recorded as missed. The check-ins run on here after the
    response, as detached turns, so this answers in a second or two.
    """
    out = await checkins.run_due()
    started = [CheckinStarted(checkin_id=s.checkin_id, team_id=s.team_id, kind=s.kind, date=s.nba_date)
               for s in out["started"]]
    return respond(CheckinRunResp(
        status=ApiStatus.SUCCESS,
        message="Check-ins are off" if out["off"] else f"Started {len(started)} check-in(s)",
        data=CheckinRunData(off=out["off"], teams=out["teams"], started=started, missed=out["missed"],
                            skipped=out["skipped"], stale=out["stale"]),
    ))


@router.post("/checkins/events", response_model=CheckinEventsResp)
async def checkin_events(req: CheckinEventsReq):
    """What came in about players: verdicts that changed for games not yet tipped, news items just
    stored. Each becomes an event on every team Dime runs that rosters the player, and a team with a
    new one gets a check-in about it (or the scheduled one about to run takes it).

    Called by data-platform's availability pipeline (its late-scratch events, every 2-minute tick that
    has one) and its news feed (every 5 minutes). Keys only: the verdicts and items are read here. Only
    teams on AUTOPILOT_TEAM_IDS with Late scratches at AI hear them; the check-ins run on here after
    the response.
    """
    out = await events.receive([(v.player_id, v.game_date) for v in req.availability], req.news)
    started = [CheckinStarted(checkin_id=s.checkin_id, team_id=s.team_id, kind=s.kind, date=s.nba_date)
               for s in out["started"]]
    return respond(CheckinEventsResp(
        status=ApiStatus.SUCCESS,
        message="Events are off" if out["off"] else f"Recorded {out['recorded']} event(s)",
        data=CheckinEventsData(off=out["off"], teams=out["teams"], facts=out["facts"], recorded=out["recorded"],
                               started=started),
    ))


@router.post("/valuation/standard", response_model=StandardValuationResp)
async def value_standard_league(req: StandardValuationReq):
    """Rank a set of projections in the standard league, in points and in 9-cat.

    Called by data-platform's projections editor, which holds the projections
    (and the edit being previewed) and has no valuation of its own. The ranks
    are the `cv_rank` a room with no league would show for the same
    projections: ESPN's default points weights or the standard nine categories,
    twelve teams, thirteen rounds, the default fantasy-playoff weeks. Nothing
    is read from or written to the database.
    """
    weight = playoff_weight_of(req.playoff_weight)
    calendar = season_calendar()
    lines = [
        ProjectedLine(
            player_id=p.player_id, line=p.line, games=p.games, team=p.team,
            dd_rate=p.dd_rate, td_rate=p.td_rate,
        )
        for p in req.players
    ]
    ranks = await run_cpu("valuation.standard", standard_ranks, lines, calendar, weight)
    return respond(StandardValuationResp(
        status=ApiStatus.SUCCESS,
        message=f"Valued {len(ranks)} players in the standard league",
        data=StandardValuationData(
            league_size=STANDARD_LEAGUE_SIZE,
            rounds=STANDARD_ROUNDS,
            playoff_weight=weight,
            playoff_weeks=list(standard_playoff_weeks(calendar)),
            players=[StandardRankResp(**vars(rank)) for rank in ranks],
        ),
    ))

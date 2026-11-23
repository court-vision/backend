"""
Service-to-service jobs, authenticated with the shared pipeline bearer token
(the same `PIPELINE_API_TOKEN` cron-runner presents to data-platform). No user
identity: the caller names the user and team, and ownership is re-checked here.
"""

from dataclasses import asdict

from fastapi import APIRouter, Query, Security

from core.compute import run_cpu
from core.pipeline_auth import verify_pipeline_token
from core.responses import respond
from schemas.ai_spend import AiCapReq, AiCapResp, AiSpendResp
from schemas.analyst import AnalystReadReq, AnalystReadResp, AnalystSpendResp
from schemas.autopilot import (
    CheckinEventsData, CheckinEventsReq, CheckinEventsResp, CheckinRunData, CheckinRunResp, CheckinStarted,
)
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupEvaluateReq, LineupEvaluationResp, LineupReactReq, LineupReactResp
from schemas.move_outcomes import LedgerRunReq, LedgerRunResp
from schemas.scheduled_pickup import PickupExecuteReq, PickupExecuteResp
from schemas.valuation import (
    DailyRankingsData,
    DailyRankingsResp,
    StandardRankResp,
    StandardValuationData,
    StandardValuationReq,
    StandardValuationResp,
)
from services.ai import analyst, checkins, events, spend_report
from services.late_scratch_service import LateScratchService
from services.lineup_editor_service import LineupEditorService
from services.move_ledger_service import MoveLedgerService
from services.scheduled_pickup_service import ScheduledPickupService
from services.valuation import daily as daily_boards
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
    teams of accounts with CV AI, with Late scratches at AI, hear them; the check-ins run on here after
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


@router.post("/rankings/daily", response_model=DailyRankingsResp)
async def build_daily_rankings(
    shadow: bool = Query(
        False,
        description="Build the boards on the projection that has the analyst's notes applied (cv_live_shadow).",
    ),
):
    """Today's two standard boards (points and 9-cat), for the data platform to store.

    Called by data-platform's daily_rankings pipeline each morning, which writes
    them to nba.daily_rankings beside ESPN's rank that day. The boards are the
    ones a room with no league shows right now: in season the morning's live
    projection (the pipeline builds it first), or with no fresh one the
    projection blended with this season's games; before opening night the
    projection alone. `model` says which. `in_season` is false outside the
    regular season, and the pipeline stores nothing then. Nothing is written here.

    `shadow=true` is the analyst's second pair (the Live rankings plan, step 4):
    the same boards built on the projection with its notes applied, which the
    data platform's analyst pipeline stores under the source `cv_shadow`. No
    user-facing board reads that projection.
    """
    boards = await daily_boards.today(shadow=shadow)
    return respond(DailyRankingsResp(
        status=ApiStatus.SUCCESS,
        message=f"Ranked {len(boards.players)} players as of {boards.as_of.isoformat()}",
        data=DailyRankingsData(league_size=STANDARD_LEAGUE_SIZE, rounds=STANDARD_ROUNDS, **asdict(boards)),
    ))


@router.post("/analyst/read", response_model=AnalystReadResp)
async def analyst_read(req: AnalystReadReq):
    """One read of one player by the live rankings' analyst (the Live rankings plan, step 4).

    Called by data-platform's analyst pipeline for each player its morning
    screen picks, with what the live projection assumes about him and the stored
    items about him. The answer is a proposal in the projection's own inputs (a
    return date, minutes, a share of the offense, an end, the items it rests
    on), or that nothing should be written. Nothing is written here: the data
    platform checks the proposal against its stored items and bounds and owns
    the tables. `data.outcome` carries every business result (`read`, `declined`,
    `malformed`, `budget`, `off`); only a provider failure is an error, and the
    pipeline reads the player again on its next run.
    """
    data = await analyst.read(req)
    return respond(AnalystReadResp(status=ApiStatus.SUCCESS, message=f"Analyst read: {data.outcome}", data=data))


@router.get("/analyst/spend", response_model=AnalystSpendResp)
async def analyst_spend():
    """What the analyst's reads have spent this month and what they may, for the
    data platform's morning digest."""
    data = await analyst.month()
    return respond(AnalystSpendResp(status=ApiStatus.SUCCESS, message="Analyst spend this month", data=data))


@router.get("/ai/spend", response_model=AiSpendResp)
async def ai_spend(days: int = Query(30, ge=1, le=spend_report.MAX_DAYS,
                                     description="How many UTC days back, today included")):
    """What AI has spent, for the data platform's AI spend page: this month against
    each line's cap (the desk, Dime's check-ins, the analyst) and against the whole
    app's, the ledger over the window split by day, feature, user and team, what each call's record says it used
    (model and tokens), and every user with spend or a cap, with their caps."""
    data = await spend_report.report(days)
    return respond(AiSpendResp(status=ApiStatus.SUCCESS, message=f"AI spend over {days} days", data=data))


@router.put("/ai/caps", response_model=AiCapResp)
async def ai_cap(req: AiCapReq):
    """Set a line's monthly cap, the whole app's (`line` app), or one user's cap on the
    desk or autopilot line, from the next reservation on. `monthly_usd` null clears it:
    the environment's number applies again (and a user's desk cap is an equal part of
    the desk's)."""
    data = await spend_report.set_cap(req)
    what = ("whole app's cap" if req.line == "app" else f"{req.line} cap") + (
        f" for user {req.user_id}" if req.user_id is not None else "")
    message = f"Cleared the {what}" if req.monthly_usd is None else f"Set the {what} to ${req.monthly_usd}"
    return respond(AiCapResp(status=ApiStatus.SUCCESS, message=message, data=data))

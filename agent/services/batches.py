"""Placing Twilio calls, and the batch dialer built on top of it.

A batch is one uploaded claims sheet dialed top to bottom with a single
click. Calls go out one at a time, never in parallel: agent/state.py and
main.py's call_results.json describe "the current call", so the whole app is
single-call by design. The loop here places a call, waits until Twilio says
that call is finished for good, and only then moves to the next row.

The loop is a plain asyncio task inside the web process, so it dies with the
process -- reconcile_orphaned_batches() cleans up after a restart.
"""

import asyncio
import os
import time

from django.utils import timezone
from loguru import logger
from twilio.rest import Client as TwilioClient

from main import clear_results, load_results, outcome_from_captured, save_call_outcome_if_unset

from agent.models import CallBatch, CallLog
from agent.state import (
    background_tasks,
    batch_state,
    call_claim_context,
    call_pending_extension,
    call_runners,
    call_terminal_status,
    state,
)

# Twilio's final CallStatus values -> our call_outcome. "completed" only
# means the call connected and has now ended; what happened on it is recorded
# by the pipeline (success/partial when Emily ends it, ivr_dead_end_needs_
# callback, error). When the pipeline recorded nothing -- the rep hung up, or
# the silence timeout ended it -- the outcome is judged from what was
# captured instead (main.outcome_from_captured); "incomplete" is only the
# placeholder until then.
TWILIO_TERMINAL_OUTCOMES = {
    "completed": CallLog.OUTCOME_INCOMPLETE,
    "no-answer": "no_answer",
    "busy": "busy",
    "failed": "failed",
    "canceled": "canceled",
}

PAUSE_BETWEEN_CALLS = 3  # seconds
# The /call-status webhook is the normal way we learn a call is over. If it
# never arrives (ngrok down, say) the batch would wait forever, so Twilio is
# also asked directly this often.
STATUS_POLL_INTERVAL = 20  # seconds
# How long to wait for /ws to finish tearing down once Twilio has reported
# the call over, before moving on regardless.
WS_TEARDOWN_GRACE = 60  # seconds
# A /ws leg still running this long after Twilio reported its call over is
# cancelled -- see _cancel_lingering_pipelines.
LINGERING_PIPELINE_GRACE = 5  # seconds

# Outcomes that are only a judgement of the captured data, which a later
# sync with more data may revise. Anything else (ivr_dead_end_needs_callback,
# error, no_answer...) is a fact about how the call went and is left alone.
JUDGED_OUTCOMES = ("", CallLog.OUTCOME_INCOMPLETE, CallLog.OUTCOME_PARTIAL, "success")


def call_in_progress() -> bool:
    return state["status"] in ("starting", "in_progress")


def batch_running() -> bool:
    task = batch_state["task"]
    return batch_state["starting"] or (task is not None and not task.done())


async def dial_call(destination_number, extension, claim_context, user, call_log=None):
    """Place one real call through Twilio and record it.

    Pass call_log to dial an existing (queued) batch row; otherwise a new
    CallLog is created. Returns (call_log, error) -- error is a message for
    the user and call_log is None when the call couldn't be placed.
    """
    base_url = os.getenv("PUBLIC_BASE_URL")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")
    if not all([base_url, account_sid, auth_token, from_number]):
        return None, (
            "Set PUBLIC_BASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_PHONE_NUMBER in .env first."
        )

    clear_results()
    state.update(
        status="starting",
        runner=None,
        error=None,
        destination_number=destination_number,
        extension=extension,
        claim_context=claim_context,
        dialed_call_sid=None,
    )

    client = TwilioClient(account_sid, auth_token)
    try:
        call = await asyncio.to_thread(
            client.calls.create,
            to=destination_number,
            from_=from_number,
            url=f"{base_url}/twiml",
            status_callback=f"{base_url}/call-status",
            status_callback_event=["completed"],
            record=True,
            recording_status_callback=f"{base_url}/recording-status",
            recording_status_callback_event=["completed"],
        )
    except Exception as exc:  # noqa: BLE001 -- surface Twilio API errors to the UI
        state.update(status="error", error=str(exc))
        return None, str(exc)

    state["dialed_call_sid"] = call.sid
    call_pending_extension[call.sid] = extension
    call_claim_context[call.sid] = claim_context

    if call_log is None:
        call_log = await CallLog.objects.acreate(
            call_sid=call.sid,
            destination_number=destination_number,
            extension=extension,
            claim_context=claim_context,
            placed_by=user,
        )
    else:
        call_log.call_sid = call.sid
        call_log.started_at = timezone.now()
        await call_log.asave(update_fields=["call_sid", "started_at"])
    return call_log, None


async def sync_call_results(call_sid: str, call_over: bool):
    """Copy call_results.json into the call's CallLog row.

    Runs when each /ws leg tears down (which includes the mid-call DTMF
    redirect, not only the real end) and when Twilio reports the call over.
    Fields are merged, never replaced, and the file is only read while it
    still belongs to this call: a leg that shuts down late, after the next
    call has been placed and the file cleared, must not wipe out -- or worse,
    take over -- what this call captured. That happened in a real batch.

    call_over: Twilio has said this call is finished. Only then is a missing
    outcome judged from the captured data.
    """
    row = await CallLog.objects.filter(call_sid=call_sid).afirst()
    if row is None:
        return

    pipeline_outcome = ""
    fields = dict(row.fields_captured)
    if state["dialed_call_sid"] == call_sid:
        results = load_results()
        pipeline_outcome = results.get("call_outcome", {}).get("value", "")
        fields.update({k: v["value"] for k, v in results.items() if k != "call_outcome"})

    update = {}
    if fields != row.fields_captured:
        update["fields_captured"] = fields
    if pipeline_outcome:
        outcome = pipeline_outcome
    elif call_over and row.call_outcome in JUDGED_OUTCOMES:
        outcome = outcome_from_captured(fields)
    else:
        outcome = row.call_outcome
    if outcome != row.call_outcome:
        update["call_outcome"] = outcome
    if outcome and row.ended_at is None:
        update["ended_at"] = timezone.now()
    if update:
        await CallLog.objects.filter(pk=row.pk).aupdate(**update)


async def _cancel_lingering_pipelines(call_sid: str):
    """A /ws leg doesn't always notice that Twilio has closed its websocket
    -- in a real batch, legs kept running for over a minute after their call
    ended (until the silence timeout, presumably). Once Twilio says the call
    is over there is nothing left for them to do, so cancel them."""
    await asyncio.sleep(LINGERING_PIPELINE_GRACE)
    for runner in list(call_runners.get(call_sid, ())):
        logger.warning(f"{call_sid}: pipeline still running {LINGERING_PIPELINE_GRACE}s after the call ended, cancelling it")
        try:
            await runner.cancel(reason="call already ended")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{call_sid}: cancelling pipeline failed: {exc}")


async def record_twilio_terminal_status(call_sid: str, twilio_status: str):
    """Twilio has reported a call as over. Called from the /call-status
    webhook and, as a fallback, from the batch loop's own status check --
    safe to call more than once for the same call."""
    outcome = TWILIO_TERMINAL_OUTCOMES.get(twilio_status)
    if outcome is None or not call_sid:
        return
    call_terminal_status[call_sid] = twilio_status

    # A late callback for an earlier call must not touch the current call's
    # live state. dialed_call_sid is None after a server restart, in which
    # case there is no current call to protect.
    if state["dialed_call_sid"] in (None, call_sid):
        if twilio_status != "completed":
            save_call_outcome_if_unset(outcome)
        # A call that never connected never reaches /ws, which is what would
        # otherwise move the status on from "starting".
        if state["status"] == "starting":
            state["status"] = "ended"

    if twilio_status == "completed":
        # Doesn't wait for /ws to tear down: everything the call captured is
        # already in call_results.json.
        await sync_call_results(call_sid, call_over=True)
        if call_runners.get(call_sid):
            task = asyncio.create_task(_cancel_lingering_pipelines(call_sid))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
        return
    await CallLog.objects.filter(call_sid=call_sid, call_outcome="").aupdate(
        call_outcome=outcome, ended_at=timezone.now()
    )


async def _fetch_twilio_status(call_sid: str) -> str | None:
    client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    try:
        call = await asyncio.to_thread(lambda: client.calls(call_sid).fetch())
    except Exception as exc:  # noqa: BLE001 -- just try again on the next poll
        logger.warning(f"batch: couldn't fetch status of {call_sid} from Twilio: {exc}")
        return None
    return call.status


async def _wait_for_call_to_finish(call_sid: str):
    last_poll = time.monotonic()
    terminal_since = None
    while True:
        twilio_status = call_terminal_status.get(call_sid)
        now = time.monotonic()
        if twilio_status is None and now - last_poll >= STATUS_POLL_INTERVAL:
            last_poll = now
            fetched = await _fetch_twilio_status(call_sid)
            if fetched in TWILIO_TERMINAL_OUTCOMES:
                logger.warning(f"batch: no status callback for {call_sid}, Twilio reports {fetched!r}")
                await record_twilio_terminal_status(call_sid, fetched)
                twilio_status = fetched
        if twilio_status is not None:
            terminal_since = terminal_since or now
            if state["ws_active"] <= 0:
                break
            if now - terminal_since > WS_TEARDOWN_GRACE:
                logger.warning(f"batch: /ws still open {WS_TEARDOWN_GRACE}s after {call_sid} ended, moving on")
                break
        await asyncio.sleep(1)

    # The webhook can beat dial_call to saving the row's call_sid, in which
    # case its database update matched nothing. Apply it again now.
    await record_twilio_terminal_status(call_sid, twilio_status)


async def _mark_uncalled_rows(batch_id: int):
    await CallLog.objects.filter(batch_id=batch_id, call_sid__isnull=True, call_outcome="").aupdate(
        call_outcome=CallLog.OUTCOME_NOT_CALLED
    )


async def run_batch(batch_id: int):
    """Dial every queued row of a batch, in sheet order. Runs as a
    background task -- see start_batch()."""
    final_status = CallBatch.STATUS_COMPLETED
    try:
        batch = await CallBatch.objects.select_related("created_by").aget(id=batch_id)
        rows = [row async for row in CallLog.objects.filter(batch_id=batch_id).order_by("row_number", "id")]
        for index, call_log in enumerate(rows):
            # A manually placed call may still be going from before the batch
            # started.
            while call_in_progress() and not batch_state["stop_requested"]:
                await asyncio.sleep(1)
            if batch_state["stop_requested"]:
                final_status = CallBatch.STATUS_STOPPED
                break

            _, error = await dial_call(
                call_log.destination_number,
                call_log.extension,
                call_log.claim_context,
                batch.created_by,
                call_log=call_log,
            )
            if error:
                # One bad number shouldn't end the batch -- note it on the
                # row and carry on.
                logger.warning(f"batch {batch_id}: row {call_log.row_number} not dialed: {error}")
                call_log.call_outcome = "failed"
                call_log.error = error
                call_log.ended_at = timezone.now()
                await call_log.asave(update_fields=["call_outcome", "error", "ended_at"])
                continue

            await _wait_for_call_to_finish(call_log.call_sid)
            if index < len(rows) - 1:
                await asyncio.sleep(PAUSE_BETWEEN_CALLS)
    except Exception:  # noqa: BLE001 -- a background task has nowhere to raise to
        logger.exception(f"batch {batch_id}: dialer crashed")
        final_status = CallBatch.STATUS_INTERRUPTED
    finally:
        await _mark_uncalled_rows(batch_id)
        await CallBatch.objects.filter(id=batch_id).aupdate(status=final_status, finished_at=timezone.now())
        batch_state.update(batch_id=None, task=None, stop_requested=False)
        logger.info(f"batch {batch_id}: finished ({final_status})")


async def start_batch(claims: list[dict], user, source_filename: str = "") -> CallBatch:
    # Set before the first await, so nothing else (a second Start click, or
    # reconcile_orphaned_batches seeing a running batch with no task yet)
    # can get in while the rows are being created.
    batch_state["starting"] = True
    try:
        batch = await CallBatch.objects.acreate(source_filename=source_filename, created_by=user)
        await CallLog.objects.abulk_create(
            [
                CallLog(
                    batch=batch,
                    row_number=claim["row_number"],
                    destination_number=claim["destination_number"],
                    extension=claim["extension"],
                    claim_context=claim["claim_context"],
                    placed_by=user,
                )
                for claim in claims
            ]
        )
        batch_state.update(batch_id=batch.id, stop_requested=False, upload_batch_id=batch.id)
        batch_state["task"] = asyncio.create_task(run_batch(batch.id))
    finally:
        batch_state["starting"] = False
    return batch


async def reconcile_orphaned_batches():
    """A batch still marked running with no task behind it was cut off by a
    server restart. Close it out so it stops showing as running forever; the
    rows it never reached become not_called."""
    if batch_running():
        return
    async for batch in CallBatch.objects.filter(status=CallBatch.STATUS_RUNNING):
        await _mark_uncalled_rows(batch.id)
        batch.status = CallBatch.STATUS_INTERRUPTED
        batch.finished_at = timezone.now()
        await batch.asave(update_fields=["status", "finished_at"])

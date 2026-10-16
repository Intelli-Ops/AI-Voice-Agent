# AI Voice Agent — Project Handoff / Continuity Notes

Read this first if picking the project back up on a new machine, in a new
Claude session, or after a long gap. Written to answer "what is this, what's
been built, what decisions were made and why, what's left."

## What this is

An AI voice agent for a medical lien management company. It places phone
calls to insurance companies' claims departments, navigates their phone
menus (IVR) automatically, talks to a human rep once it reaches one, and
collects claim verification info (claim status, adjuster name/phone/email/
fax). Built on Pipecat (voice AI pipeline framework) + Twilio (telephony) +
Deepgram (STT) + Cartesia (TTS) + OpenAI (LLM, model `gpt-6-luna`).

## Current state

- **Active branch: `django-migration`** — this is where all current work
  lives. `main` is the old FastAPI version, intentionally left behind as a
  reference point; `django-migration` has not been merged back into it yet.
- The app was originally a single-file FastAPI prototype (`app.py` +
  `main.py`). It has since been fully converted to Django. `main.py` is
  still the core pipeline/business logic and is framework-agnostic by
  design — Django just calls into it.

## Architecture

```
voiceagent/           Django project config (settings, urls, asgi)
agent/                Django app -- all the actual views/models/templates
  models.py            CallBatch, CallLog, Recording, GoogleDriveAuth, Profile (role)
  views.py             Every HTTP endpoint (dial page, APIs, webhooks, admin pages)
  urls.py
  decorators.py         role_required() -- admin/user gate on top of login_required
  state.py              In-memory, process-local dicts (call status, pending
                         extension/claim_context, uploaded claims) -- deliberately
                         NOT in the database; this is still a single-call-at-a-time
                         prototype, not a durable multi-call system
  ws_server.py           Twilio Media Stream /ws handler -- raw ASGI, NOT Django
                         Channels (see "Key decisions" below for why)
  services/recordings.py The Twilio -> Google Drive recording pipeline
  services/batches.py    dial_call() (every real call goes through it) and the
                         batch dialer loop
  services/exports.py    Call results -> .xlsx for the admin download
  templates/agent/        index.html (dial page), login.html, call_logs.html (admin)
main.py                The actual Pipecat pipeline: STT/LLM/TTS wiring, IVR
                        navigation, DTMF dialing, prompt construction, tool
                        calls (save_field, save_adjuster_info, end_call),
                        call_outcome tracking, Excel claim-sheet parsing
run.py                 Runs the app via uvicorn against the ASGI app (NOT
                        `manage.py runserver` -- that can't carry websockets)
```

## Features built so far (in order)

1. **Stuck-call escalation** — `<ivr>stuck</ivr>` now hangs up the call and
   marks it `ivr_dead_end_needs_callback` instead of silently doing nothing.
2. **Call outcome classification** — every call ends with a `call_outcome`
   (`success` / `no_answer` / `busy` / `failed` / `canceled` /
   `ivr_dead_end_needs_callback` / `error`), including calls that are never
   answered at all (via Twilio's own status callback webhook, since those
   never reach our pipeline).
3. **Dynamic claim data from Excel** — claim info (client, claim number,
   DOI, patient name) comes from an uploaded `.xlsx` sheet instead of being
   hardcoded. Fixed template columns: `destination_number`, `extension`,
   `client_name`, `claim_number`, `doi`, `patient_name`.
4. **FastAPI → Django conversion** — see "Key decisions" below for the
   websocket handling approach.
5. **MySQL 8** (was SQLite) — per CTO's request.
6. **Login + 2 roles (admin/user) + admin call-log page** — Django's
   built-in auth, a `Profile.role` field, `/call-logs` admin-only page
   showing every call: destination, outcome, timestamps, who placed it.
7. **Twilio call recording → Google Drive pipeline** — records every real
   call, uploads the audio to Drive, deletes it from Twilio once the
   upload is confirmed, and plays it back from the admin call-log page
   (never a public Drive link — streamed through an admin-only endpoint).

8. **Batch dialer + Excel export** — upload the sheet, every row is listed
   on the dial page, "Call All Rows" dials them one after another (never
   in parallel) and saves each row's status and captured fields as it
   goes. How a row's outcome is decided: see "Outcome is judged on the
   data captured" under Key decisions. "Stop Batch" lets the current call finish and marks the rest
   `not_called`. Admins download results as `.xlsx` from `/call-logs`
   (one batch, or every call). The single-row "Place Real Call" flow is
   still there. Verified with Twilio faked out; **not yet run against
   real calls.**

## Not yet built / not yet verified

- **Recording upload has never succeeded end to end.** Drive is connected
  (OAuth as `asva.kamal@appedology.com`), but that account had no access
  to the Shared Drive/folder in `.env` when last checked — the CTO needs
  to add it as a member (Contributor+), or authorize with their own
  account at `/google-drive/authorize`.
- MySQL is wired up, but no code depends on anything MySQL-specific beyond
  standard Django ORM — moving back to another DB would be trivial if ever
  needed.

## Key decisions and why (don't redo these without good reason)

- **Django, not Django Channels, for the Twilio websocket.** Pipecat's
  `FastAPIWebsocketTransport` expects an object shaped exactly like
  Starlette's `WebSocket` (`receive()`/`send_bytes()`/`close()`/etc.).
  Channels' consumer interface doesn't match that at all. Instead,
  `voiceagent/asgi.py` runs a tiny raw-ASGI dispatcher: requests to `/ws`
  go to `agent/ws_server.py` (which wraps the raw ASGI scope in an actual
  `starlette.websockets.WebSocket`), everything else goes to Django's own
  ASGI app. This let pipecat's transport layer work completely unmodified.
- **Django pinned to 5.2, not 6.x.** Django 6.1 requires MySQL 8.4+; the
  MySQL Installer gave us 8.0.46. Rather than forcing a MySQL upgrade,
  Django was downgraded to the 5.2 LTS line, which fully supports MySQL
  8.0+. No app code depends on anything 6.x-only.
- **`mysqlclient`, not `PyMySQL`.** A precompiled wheel was available for
  this Python/Windows combo, so no C build tooling was actually needed —
  got the faster driver for free.
- **Google Drive uses OAuth, not a service account.** Service accounts
  have zero storage quota of their own and can't write into a regular
  Drive folder, even one shared with Editor access — only into a *Shared
  Drive*. Creating a Shared Drive turned out to be admin-restricted on the
  org's Google Workspace. The CTO instead set up an OAuth client, so
  uploads act as a real authorized user's own Google account (full quota).
  The refresh token from that one-time consent flow lives in the
  `GoogleDriveAuth` singleton DB row. Re-authorize anytime at
  `/google-drive/authorize` (admin-only).
- **OAuth redirect URI is `https://<ngrok-domain>/callback`, exact match
  required.** This is registered in the Google Cloud Console OAuth client
  and must match byte-for-byte. It currently depends on keeping the same
  ngrok reserved domain (`president-unruly-fossil.ngrok-free.dev`) alive —
  if that ever changes, the CTO needs to update the registered redirect
  URI in Google Cloud Console to match, and `.env`'s
  `GOOGLE_DRIVE_REDIRECT_URI` needs to match it exactly.
- **Django's `LogoutView` requires POST in Django 5+** (GET-based logout
  was removed). Both "Log Out" links are small `<form method="post">`
  blocks styled to look like links, not `<a href>` tags.
- **`CallLog`/`Recording` rows get synced from `call_results.json`,
  not written to directly by `main.py`.** `main.py` remains
  framework-agnostic — it still just reads/writes a local JSON file for
  the live in-call UI polling. Django syncs that snapshot into the
  database at genuine completion points (never mid-call, since the same
  teardown path in `ws_server.py` also fires on the DTMF-redirect
  reconnect, not just the true end of a call — see the comment on
  `_sync_call_log` in `agent/ws_server.py`).

- **A call is only "over" when Twilio says so, not when `/ws` closes.**
  `/ws` also disconnects mid-call on a DTMF redirect. The batch loop waits
  for Twilio's final status (the `/call-status` webhook, or a direct
  status fetch every 20s in case the webhook is lost) *and* for `/ws` to
  finish syncing results to the database before it clears
  `call_results.json` and dials the next row. A connected call that ends
  with no outcome from the pipeline (e.g. the rep hung up) is judged from
  what it captured (next point).
- **The batch loop is an asyncio task inside the web process.** A server
  restart kills it — and `run.py` uses `reload=True`, so *saving any
  `.py` file while a batch is running stops the batch*. The next page
  load marks such a batch `interrupted` and its remaining rows
  `not_called`; it is not resumed.
- **Two statuses: Verified / Unverified (CTO's decision, 2026-10-16).**
  Verified = the rep confirmed the claim; adjuster details are not
  required. Everything else is Unverified, with the reason stored in
  `call_outcome` (claim not confirmed, stuck in phone menu, no answer,
  busy, call failed, canceled, not called, system error). Judged by
  `main.outcome_from_captured()` from the data captured, however the call
  ended. Labels live in `CallLog.OUTCOME_LABELS`.
- **Emily's opening line runs with no tools.** With tools attached the
  model sometimes called one in the same reply as its greeting (saving
  "claim confirmed" before the rep spoke, or end_call), and Pipecat's
  re-run for the tool result made her repeat the whole greeting or stall.
  `ContextTrimmer._maybe_enable_tools` switches tools on from the turn
  after the opening.
- **Known extension timing.** Sent 5s after the /ws leg connects
  (`EXTENSION_DIAL_DELAY`), not 5s after the menu is detected (detection
  itself takes ~5s; the test line's menu had given up by then). The
  second leg may redial once if the menu asks again
  (`MAX_EXTENSION_LEGS`).

## Environment variables needed (`.env`, never committed)

```
DEEPGRAM_API_KEY, OPENAI_API_KEY, CARTESIA_API_KEY, CARTESIA_VOICE_ID
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
PUBLIC_BASE_URL                          (the ngrok https URL)
MYSQL_DB, MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST, MYSQL_PORT
GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET
GOOGLE_DRIVE_REDIRECT_URI                (must exactly match the Google Cloud Console entry)
GOOGLE_DRIVE_SHARED_DRIVE_ID, GOOGLE_DRIVE_FOLDER_ID
```

## Accounts / external resources involved

- **Twilio** — phone number, calling account.
- **Deepgram, OpenAI, Cartesia** — API keys for STT/LLM/TTS.
- **MySQL** — local server, one per machine (the data does not travel
  with git; a new machine starts empty unless a `mysqldump` is restored).
  Database `voiceagent`, app user `voiceagent_app`.
- **Google Cloud project** `voice-agent-recordings` — OAuth client for
  Drive access; CTO manages this.
- **Google Workspace Shared Drive** — holds the actual recording files;
  CTO created it and added the OAuth-authorized user.
- **ngrok** — reserved free static domain
  `president-unruly-fossil.ngrok-free.dev`, needed so the Twilio webhooks
  and Google OAuth redirect URI don't need reconfiguring every restart.

## Test accounts that exist right now (change these before real use)

Accounts live in each machine's own database, so these differ per machine.
A new database needs `python manage.py createsuperuser` and then the
account's role set to `admin` (new accounts default to `user`), e.g. via
`/admin/` → Users → the user's Profile.

- `admin` — role `admin`
- `rep1` — role `user` (for testing role gating)

## How to run it

See `HANDOFF.md`'s sibling conversation for the full new-machine setup
checklist (clone → venv → MySQL → `.env` → migrate → createsuperuser →
ngrok → `python run.py`). Short version once everything's installed:

```bash
venv\Scripts\activate
python run.py                 # serves on localhost:8000 via uvicorn (NOT manage.py runserver)
```
In a second terminal, for real Twilio calls / Google OAuth:
```bash
ngrok http --domain=president-unruly-fossil.ngrok-free.dev 8000
```

## Things to know that aren't bugs, just prototype-level tradeoffs

- Only one call can be in progress at a time (`agent/state.py`'s module-
  level dicts are process-global, not per-call). This is intentional for
  now and fits the still-undone batch-dialer design (sequential, not
  concurrent).
- `ALLOWED_HOSTS = ["*"]` and a fallback `DJANGO_SECRET_KEY` — fine for an
  internal prototype behind a changing ngrok URL, not production-hardened.
- API endpoints hit via `fetch()` are `@csrf_exempt` by design (no token
  available to a bare JS fetch call); Twilio's webhooks are also
  unauthenticated by necessity (Twilio can't supply a session cookie).

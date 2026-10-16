# Feature Handover: Excel Export, Verified/Unverified Statuses, Excel Date Reading

This document covers three self-contained features of the AI Voice Agent:
what each one does, where its code lives, how it works, the background
knowledge needed to understand it, how to test it, and how to change it.

| # | Feature | Size | Main files |
|---|---|---|---|
| 1 | Excel export of call results | ~100 lines | `agent/services/exports.py`, `agent/views.py`, `agent/templates/agent/call_logs.html` |
| 2 | Verified / Unverified call statuses, plus rep name and reference number | ~80 lines | `main.py`, `agent/models.py`, `agent/services/batches.py` |
| 3 | Reading Excel dates and numbers correctly | ~15 lines | `main.py` |

For the whole system (architecture, WebSockets, models, every view), see
`PROJECT_GUIDE.md`. This document assumes only what is explained below.

---

## Part 0: Base knowledge

### The project in one minute

An AI agent ("Emily") phones insurance companies to verify medical lien
claims. A user uploads an Excel sheet of claims; the app calls each row
through **Twilio**; the conversation runs through a **Pipecat** pipeline
(Deepgram speech-to-text → OpenAI `gpt-6-luna` → Cartesia text-to-speech).
What Emily collects is saved per call in **MySQL** through **Django**, and
admins review and export it on the Call Logs page.

### Running it locally

```bash
venv\Scripts\activate
python run.py          # serves http://localhost:8000 (restart after code changes)
```

Real phone calls also need `ngrok http --url=president-unruly-fossil.ngrok-free.dev 8000`
in a second terminal. Features 1 and 3 can be tested without placing any
real call.

### Concepts used in these features

**Django request flow.** A browser requests a URL → `agent/urls.py` maps it
to a function in `agent/views.py` → the view queries the database through
models and returns an `HttpResponse` (a page, JSON, or a file).

```python
# agent/urls.py
path("call-logs/export", views.export_call_logs, name="call-logs-export"),
```

The `name` lets templates link to it with `{% url 'call-logs-export' %}`
instead of hard-coding the path.

**Django ORM.** Database rows are Python objects. `CallLog.objects.filter(batch_id=3)`
is a lazy query (a *queryset*); it runs only when iterated.
`select_related("placed_by")` joins a foreign key in the same SQL query;
`prefetch_related("recordings")` loads a reverse relation in one extra query.
Both avoid the *N+1 problem*: one query per row when looping.

**Decorators for access.** `@role_required("admin")` (in
`agent/decorators.py`) sends anonymous users to the login page and gives
non-admins a 403. Roles live in `Profile.role`.

**`CallLog` model** (`agent/models.py`). One row per real call:
`destination_number`, `extension`, `claim_context` (JSON: client, claim
number, DOI, patient), `call_outcome`, `fields_captured` (JSON of what Emily
collected), `error`, `started_at`, `ended_at`, `placed_by`, and for batch
calls `batch` and `row_number`. Its `status_label` property returns the
outcome, or "in progress" / "queued".

**`call_results.json`.** While a call runs, Emily's tools write each confirmed
answer to this file in the project root:

```json
{
  "claim_confirmed": {"value": "yes", "captured_at": "2026-10-15T06:59:10+00:00"},
  "adjuster_name":   {"value": "Dana Reyes", "captured_at": "..."},
  "call_outcome":    {"value": "verified", "captured_at": "..."}
}
```

`main.py` (the voice pipeline) only knows this file, never the database.
Django copies it into the `CallLog` row (`sync_call_results()` in
`agent/services/batches.py`).

**LLM tool calling.** The model is given "tools" (functions with a name,
description and JSON parameters). When the rep confirms something, the model
replies with a tool call instead of speech, e.g.
`save_field(field_name="claim_status", value="open")`; Pipecat runs the
matching Python handler, which saves the value. Tool definitions are
`FunctionSchema` objects in `main.py`.

**Twilio status callback.** When a call ends, Twilio sends an HTTP POST to
our `/call-status` URL with `CallStatus` = `completed`, `no-answer`, `busy`,
`failed` or `canceled`. This is the reliable signal that a call is over.

**openpyxl.** The library for reading and writing `.xlsx` files.
`load_workbook(path, read_only=True, data_only=True)` reads (data_only =
formula results, not formulas); `Workbook()` creates a new file.

---

## Feature 1: Excel export of call results

### What it does

Admins download call results as an `.xlsx` file from the Call Logs page:

- **Download Excel** next to each batch: that batch only, in sheet order.
- **Download Excel (all calls)** at the top: every call.

One line per call with: Batch, Row, Destination, Extension, Client,
Claim #, DOI, Patient, Status, Reason, one column per collected field (Claim
Confirmed, Claim Status, Adjuster Name/Phone/Email/Fax, Rep Name,
Reference #), Started (UTC), Ended (UTC), Placed By, Recording, Notes,
Call SID. ("Status" is Verified / Unverified; "Reason" says why a call is
unverified -- see Feature 2.)

### Where the code is

| File | What |
|---|---|
| `agent/services/exports.py` | Builds the workbook: `HEADERS`, `CAPTURED_FIELDS`, `_naive_utc()`, `_recording_status()`, `build_call_log_workbook()` |
| `agent/views.py` | `export_call_logs(request)` view |
| `agent/urls.py` | Route `call-logs/export`, name `call-logs-export` |
| `agent/templates/agent/call_logs.html` | The two download buttons |
| `agent/state.py` | `FIELD_LABELS`: the captured-field columns come from here |

### How it works

1. The admin clicks a link to `/call-logs/export?batch=6` (or no `?batch`).
2. `export_call_logs` (admin only) builds the queryset:

   ```python
   logs = CallLog.objects.select_related("placed_by").prefetch_related("recordings")
   if batch_id:
       logs = logs.filter(batch_id=batch_id).order_by("row_number", "id")
   ```

   An unknown or non-numeric batch ID returns 404.
3. `build_call_log_workbook(logs)`:
   - creates a workbook, writes `HEADERS` as row 1 in bold, freezes it
     (`ws.freeze_panes = "A2"`) so it stays visible when scrolling;
   - for each call appends one row: claim details from `claim_context`,
     `status_label`, each captured field from `fields_captured` (blank if
     missing), times, username, recording status, error note;
   - sets column widths;
   - saves into an in-memory `io.BytesIO` buffer and returns the bytes.
4. The view returns the bytes with the Excel content type and
   `Content-Disposition: attachment; filename="call_results_batch_6.xlsx"`,
   which makes the browser download instead of display it.

### Details worth knowing

- **Formula injection.** Anything said on a call ends up in a cell. A value
  starting with `=` (or `+`, `-`, `@`) would be treated by Excel as a
  formula. Every text cell is forced to plain text:
  ```python
  for cell in ws[ws.max_row]:
      if isinstance(cell.value, str):
          cell.data_type = "s"
  ```
- **Timezones.** Django stores times timezone-aware (UTC). Excel has no
  timezone concept and openpyxl refuses aware datetimes, so `_naive_utc()`
  strips the timezone; the headers say "(UTC)".
- **Queued rows** have a creation time, not a call time, so Started is left
  blank when there is no `call_sid`.
- **Columns follow `FIELD_LABELS`.** Adding a field there adds a column
  automatically.

### How to test

1. Log in as an admin, open Call Logs, click Download Excel (all calls).
2. Check: header row frozen, one line per call, captured fields in their
   columns.
3. Click a batch's Download Excel: only that batch, ordered by sheet row.
4. Log in as `rep1` and open `/call-logs/export`: should be 403.
5. Open `/call-logs/export?batch=99999`: should be 404.

### How to change it

- **Add a column:** add the header to `HEADERS` and the value at the same
  position in the `values` list in `build_call_log_workbook()`.
- **Change the file name:** in `export_call_logs`.

---

## Feature 2: Verified / Unverified call statuses

### The problem it solved

Calls used to end up as success / partial / incomplete, and "success" was
only given if Emily herself said goodbye and called the `end_call` tool. A
rep who gave every detail and then hung up produced "incomplete". The
statuses were also more than the business needs: the CTO asked for two.

### What it does now

Every finished call is either **Verified** or **Unverified**:

- **Verified** = the rep confirmed the claim is on file and matches (claim
  number, date of injury, patient). Adjuster details are collected when
  given, but aren't needed.
- **Unverified** comes with a reason: Claim not confirmed, Stuck in phone
  menu, No answer, Line busy, Call failed, Call canceled, Not called,
  System error.

It is decided from what was collected, however the call ended: Emily's
goodbye, the rep hanging up, the silence timeout, or the phone menu
hanging up. Emily also asks for the rep's name and a reference number
once, saves them if given (`rep_name`, `reference_number`), and says
goodbye whatever the answer.

### Where the code is

| File | What |
|---|---|
| `main.py` | `outcome_from_captured(fields, call_stage)`, `captured_fields()`, `INTERNAL_RESULT_KEYS` |
| `main.py` | `end_call` (inside `make_end_call_schema()`) saves `verified` |
| `main.py` | `_on_conversation_detected` / `_on_ivr_status_changed` record `call_stage` |
| `agent/models.py` | `CallLog.OUTCOME_*`, `OUTCOME_LABELS`, and the `verification`, `reason_label`, `status_label` properties |
| `agent/services/batches.py` | `JUDGED_OUTCOMES`, `sync_call_results()`, `record_twilio_terminal_status()` |
| `agent/migrations/0005_verified_status_and_batch_number.py` | Converted old rows: success/partial → verified, incomplete → claim_not_confirmed |
| `agent/views.py` | `_batch_payload()` sends `verification`, `outcome`, `reason` per row |
| `agent/services/exports.py` | Excel "Status" and "Reason" columns |
| `agent/templates/agent/*.html` | Verified / Unverified pills with the reason, Batch Progress bars |

### How it works

The rule is one pure function:

```python
def outcome_from_captured(fields: dict, call_stage: str = "") -> str:
    if fields.get("claim_confirmed"):
        return "verified"
    if call_stage == "phone_menu":
        return "ivr_dead_end_needs_callback"   # shown as "Stuck in phone menu"
    return "claim_not_confirmed"
```

`call_stage` is bookkeeping written to `call_results.json` by `main.py`:
`phone_menu` when an automated menu answers, `conversation` when a person
does. It is what tells "the call died in the phone menu" apart from "a
person answered but never confirmed". It is never shown as a captured
field (`captured_fields()` leaves it out).

The rule runs at the two places a call can end:

1. **Emily ends it** (`end_call`): only allowed once `claim_confirmed` is
   saved, so it saves `verified`.
2. **The call ends any other way:** Twilio POSTs `/call-status` with
   `completed` → `record_twilio_terminal_status()` →
   `sync_call_results(call_sid, call_over=True)`, which merges the captured
   fields into the row and, since the pipeline set no outcome, applies
   `outcome_from_captured`.

The database stores the precise code in `CallLog.call_outcome` (e.g.
`no_answer`); the model turns it into what people see:

```python
log.verification   # "verified" / "unverified" / "" (no result yet)
log.reason_label   # "No answer" -- only for unverified calls
log.status_label   # "Verified" / "Unverified" / "Queued" / "In progress"
```

**Judged vs factual outcomes.** `verified`, `claim_not_confirmed` and the
phone-menu result are judgements of the data and may be revised when more
data arrives (`JUDGED_OUTCOMES`). `no_answer`, `busy`, `failed`, `error`
and `not_called` are facts about the call and are never overwritten.

### How to test

Without a phone call:

```bash
python manage.py shell -c "from main import outcome_from_captured as o; print(o({'claim_confirmed':'yes'}), o({}, 'phone_menu'), o({}, 'conversation'))"
# verified ivr_dead_end_needs_callback claim_not_confirmed
```

With a real call (playing the rep):
1. Confirm the claim, refuse to give the adjuster's details → Verified.
2. Confirm the claim, then hang up before Emily's goodbye → Verified.
3. Answer but never confirm the claim, hang up → Unverified · Claim not confirmed.
4. Don't answer → Unverified · No answer.
5. Give your name and a reference number when asked → they appear as Rep
   Name and Reference # on the page and in the Excel export.

### How to change it

- **Change what counts as verified:** edit `outcome_from_captured`.
- **Rename a reason as shown to people:** edit `CallLog.OUTCOME_LABELS`.
- **Add a new captured field:** add it to the `save_field` description in
  `save_field_schema`, mention it in the prompt, and add a label in
  `FIELD_LABELS` (that also adds the Excel column and the page row).

---

## Feature 3: Reading Excel dates and numbers correctly

### The problem it solved

The claims sheet has a `doi` (date of injury) column. When that cell is a
real Excel date, openpyxl returns a Python `datetime`, and the code turned
it into text with `str()`, giving `1990-10-02 00:00:00`. Emily read exactly
that out loud on a call: "date of injury nineteen-ninety, ten, two, zero
zero zero zero zero zero". Similarly, a number typed into a numeric cell can
come back as `888.0`.

### What it does now

`_excel_cell_text(value)` in `main.py` converts each cell to the text a
person sees in Excel:

```python
def _excel_cell_text(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")        # 10/02/1990
    if isinstance(value, float) and value.is_integer():
        return str(int(value))                   # 888.0 -> "888"
    return str(value).strip()
```

It is used by the `get(key)` helper inside `load_claims_from_excel()`, so
every column (DOI, extension, phone numbers, claim numbers) goes through it.

### Background

- Excel stores a date as a number (days since 1900) plus a display format.
  openpyxl sees the format and returns a `datetime`; a date typed as text
  ("10/02/1990" with a leading apostrophe, or a text-formatted cell) stays a
  string and passes through unchanged.
- Excel stores all numbers as floating point, so whole numbers can arrive
  as `888.0`.
- US date order (MM/DD/YYYY) matches how claims staff and insurers say dates.

### How to test

1. Make a sheet with the six columns (`destination_number`, `extension`,
   `client_name`, `claim_number`, `doi`, `patient_name`), type a real date in
   `doi` and a number in `extension`.
2. Upload it on the Dialer page. The Claim dropdown and the row list should
   show the DOI as `MM/DD/YYYY` and the extension without `.0`.
3. Or in a shell:
   ```bash
   python manage.py shell -c "from main import _excel_cell_text as t; from datetime import datetime; print(t(datetime(1990,10,2)), t(888.0), t(' abc '))"
   # 10/02/1990 888 abc
   ```

### How to change it

To read dates differently (e.g. `October 2, 1990`, easier for the voice to
say), change the `strftime` pattern, for example `"%B %-d, %Y"` on Linux or
`"%B %#d, %Y"` on Windows.

---

## Glossary

| Term | Meaning |
|---|---|
| Batch | One "Call All Rows" run over an uploaded sheet (`CallBatch`) |
| call_sid | Twilio's unique ID for a call |
| Webhook / callback | An HTTP request a service (Twilio, Google) sends to our app |
| TwiML | Twilio's XML instructions for a call |
| IVR | An automated phone menu ("press 1 for claims") |
| Pipeline | The chain of audio/text processors that runs a live call (Pipecat) |
| Tool / function call | A structured action the LLM asks our code to perform |
| Queryset | A lazy Django database query |
| Migration | A versioned database schema change (`python manage.py migrate`) |
| ngrok | Gives the local app a public https address Twilio can reach |

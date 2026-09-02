# check-calc

A small Django project for splitting shared checks — restaurant bills, group
orders, anything where several people chip in — driven entirely from the Django
admin. Data lives in a local SQLite database.

**Upload a receipt — photo, scan or PDF — and it becomes a check.** An AI model
reads it and a draft check with its line items and totals appears in the admin,
ready to split. The reader is pluggable, and **two of the three options cost
nothing**.

## What it does

* **Receipt uploads** — upload a photo, a scan or a PDF and the model extracts
  the merchant, date, line items, discount, tax, tip and total. A draft check is
  created automatically and linked back to the file.
* **Checks** — a bill with a date, place, status, discount, tax % and tip %.
  Subtotal, tax, tip, total, paid and outstanding are all calculated.
* **Check items** — the individual lines, each with a unit price and quantity.
* **Shares** — who splits which item, with a weight so uneven splits work
  (`2` and `1` means two thirds / one third). Items left unassigned are shared
  by everyone on the check.
* **Payments** — what each participant actually handed over, by method.
* **Settlement** — a per-participant *items / owed / paid / balance* table on
  every check. Extras are allocated in proportion to what each person ate, and
  the rows are guaranteed to add up to the check total, cent for cent.

## Requirements

Django 6.1, which needs **Python 3.12 or newer** and SQLite 3.37 or newer.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Pick a reader (see "Which AI reads the receipt" below). The free options:
export GEMINI_API_KEY=...            # free tier, no card — aistudio.google.com/apikey
# ...or nothing at all, and run a local model with Ollama.

python manage.py migrate          # creates db.sqlite3
python manage.py seed_demo        # optional: demo participants and a check
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> — the root redirects straight to
`/admin/`.

## Which AI reads the receipt

Set `RECEIPT_PARSER_BACKEND`. All three produce the same structured result;
they differ in what they cost and where the receipt goes.

| Backend | Cost | Needs | Notes |
| --- | --- | --- | --- |
| `gemini` | **Free tier** | `GEMINI_API_KEY` from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no card | Free-tier requests are rate limited per day, and Google may use free-tier prompts to improve its products. Check the [current limits](https://ai.google.dev/gemini-api/docs/rate-limits) and [terms](https://ai.google.dev/gemini-api/terms) before sending receipts you consider private. |
| `ollama` | **Free** | [Ollama](https://ollama.com) running locally, plus `ollama pull llama3.2-vision` for photos | Nothing leaves your machine and there is no key or quota. Needs a reasonably capable machine, and small local models make more reading mistakes. |
| `claude` | Paid | `ANTHROPIC_API_KEY` | The most reliable on creased, blurred and handwritten paper. |

`auto` (the default) takes the first one that is configured: Gemini, then
Claude, then Ollama — so with no keys set at all it uses the local model.

Each backend's model is configurable: `RECEIPT_GEMINI_MODEL`
(default `gemini-2.5-flash`; `gemini-2.5-flash-lite` has a larger free daily
allowance), `RECEIPT_OLLAMA_MODEL` (default `llama3.2-vision`),
`RECEIPT_CLAUDE_MODEL` (default `claude-opus-5`).

Adding a fourth backend means writing one class with `is_configured()` and
`parse()` in `checks/parsing.py` and adding it to `BACKENDS` — the schema,
prompt, image handling and check-building are already shared.

## Uploading a receipt

Go to **Receipt uploads → Add**, pick a file, optionally choose the
participants who should be put on every item, and save. On save the app:

1. **Works out what it is holding.** A PDF with a real text layer is read as
   text — cheaper than a picture of the same words, and it cannot misread
   them. A scanned PDF is rendered to images instead (up to 3 pages, stitched
   into one). A photo is rotated upright from EXIF and downscaled to 1568px on
   the long edge.
2. **Asks the model for JSON**, with a schema, so the reply is validated
   structured data rather than prose. The same schema is translated into each
   backend's own dialect.
3. **Builds a draft check**: line items with quantities and unit prices, the
   date, the merchant as the title, and the printed discount.
4. **Reconciles it.** Printed tax and tip *amounts* are converted into the
   percentages a check stores, then everything is re-added. If the result
   misses the receipt's printed total — a misread line, or rounding — it says
   so in the check's notes instead of quietly disagreeing.

Anything the model could not read cleanly lands in the check notes and on the
upload's *What the model read* panel. Drafts are meant to be reviewed before
you settle them.

Accepted files: JPEG, PNG, WebP, GIF, BMP, TIFF and PDF.

From the command line:

```bash
python manage.py parse_receipt bill.pdf --participant Ada --participant Grace
```

If a file fails to parse the error is recorded on the upload row, not raised —
fix the file, or switch backend, and re-run the **Read selected receipts**
action.

## Admin features

| Screen | What you get |
| --- | --- |
| Check list | Status badge, item count, subtotal / total / paid / outstanding columns, date hierarchy, search, filters by status, date and settlement state |
| Check form | Line items and payments as inlines, a live totals panel and a "who owes what" settlement table |
| Check actions | Mark open, mark settled (refuses checks with money outstanding), duplicate a check with its items and shares |
| Check item form | Per-participant shares with weights |
| Receipt uploads | Thumbnail and inline preview (PDFs included), parsed line items, which backend read it, token usage, link to the created check, actions to re-read a file or re-create its check |
| Participants | Checks shared in, total paid |
| Payments | Filter and search by participant, check, method and date |

The check list computes its money columns with correlated subqueries, so the
number of database queries does not grow with the number of rows — there is a
test that keeps it that way.

## Configuration

Everything runs out of the box with development defaults. For anything beyond
local use, set these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | insecure dev key | **Set this** outside development |
| `DJANGO_DEBUG` | `True` | Turn off in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | Comma-separated origins |
| `DJANGO_DB_PATH` | `db.sqlite3` next to `manage.py` | SQLite file location |
| `DJANGO_TIME_ZONE` | `UTC` | Display time zone |
| `CHECKCALC_CURRENCY_SYMBOL` | `$` | Symbol used in admin money columns |
| `DJANGO_MEDIA_ROOT` | `media/` next to `manage.py` | Where receipt photos are stored |
| `RECEIPT_PARSER_BACKEND` | `auto` | `gemini`, `ollama`, `claude`, or `auto` |
| `GEMINI_API_KEY` | unset | Free-tier key (`GOOGLE_API_KEY` also accepted) |
| `RECEIPT_GEMINI_MODEL` | `gemini-2.5-flash` | Free-tier model to use |
| `RECEIPT_GEMINI_ENDPOINT` | Google's | Override only for a gateway or regional endpoint |
| `OLLAMA_HOST` | `http://localhost:11434` | Where your local Ollama is listening |
| `RECEIPT_OLLAMA_MODEL` | `llama3.2-vision` | Local model; must handle images for photos |
| `ANTHROPIC_API_KEY` | unset | Claude key. The SDK also accepts `ANTHROPIC_AUTH_TOKEN` or an `ant auth login` profile |
| `RECEIPT_CLAUDE_MODEL` | `claude-opus-5` | Claude model to use |
| `RECEIPT_PARSER_TIMEOUT` | `120` | Seconds to wait on whichever backend |
| `RECEIPT_PARSE_ON_UPLOAD` | `True` | Read a receipt as soon as it is uploaded |
| `RECEIPT_CREATE_CHECK_ON_PARSE` | `True` | Create the draft check straight after reading |

## Tests

```bash
python manage.py test
```

Covers the money arithmetic (rounding, largest-remainder allocation, discount
capping), the settlement rules, the receipt-to-check mapping, and the admin
screens, actions and filters.

No test calls a real API or costs anything. Each backend's request path runs
against a local stub of that service, so the request shapes — vision block,
JSON schema, downscaled image, text-vs-image routing for PDFs — are all checked
offline.

## Project layout

```
checkcalc/        project settings, URLs, WSGI/ASGI entry points
checks/
  models.py       checks, items, shares, payments, receipt uploads
  parsing.py      file preparation (image/PDF) and the model backends
  importers.py    parsed receipt -> check, items and shares
  admin.py        the whole user interface
  management/commands/   seed_demo, parse_receipt
```

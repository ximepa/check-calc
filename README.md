# check-calc

A small Django project for splitting shared checks — restaurant bills, group
orders, anything where several people chip in — driven entirely from the Django
admin. Data lives in a local SQLite database.

**Upload a photo of a receipt and it becomes a check.** Claude reads the photo,
and a draft check with its line items and totals appears in the admin, ready to
split.

## What it does

* **Receipt uploads** — photograph a paper receipt, upload it, and Claude
  extracts the merchant, date, line items, discount, tax, tip and total. A draft
  check is created automatically and linked back to the photo.
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

export ANTHROPIC_API_KEY=sk-ant-...   # needed to read receipt photos

python manage.py migrate          # creates db.sqlite3
python manage.py seed_demo        # optional: demo participants and a check
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> — the root redirects straight to
`/admin/`.

## Uploading a receipt

Go to **Receipt uploads → Add**, pick a photo, optionally choose the
participants who should be put on every item, and save. On save the app:

1. Rotates the photo upright (EXIF), downscales it to 1568px on the long edge
   and re-encodes it as JPEG — smaller upload, lower token cost.
2. Sends it to Claude with a JSON schema, so the reply is validated structured
   data rather than prose.
3. Builds a **draft** check: line items with quantities and unit prices, the
   date, the merchant as the title, and the printed discount.
4. Converts the printed tax and tip *amounts* into the percentages a check
   stores, then re-adds everything up. If the result misses the receipt's
   printed total — a misread line, or rounding — it says so in the check's
   notes instead of quietly disagreeing.

Anything the model could not read cleanly lands in the check notes and on the
upload's *What Claude read* panel. Drafts are meant to be reviewed before you
settle them.

From the command line:

```bash
python manage.py parse_receipt photo.jpg --participant Ada --participant Grace
```

If a photo fails to parse the error is recorded on the upload row, not raised —
fix the photo and re-run the **Read selected receipts with Claude** action.

## Admin features

| Screen | What you get |
| --- | --- |
| Check list | Status badge, item count, subtotal / total / paid / outstanding columns, date hierarchy, search, filters by status, date and settlement state |
| Check form | Line items and payments as inlines, a live totals panel and a "who owes what" settlement table |
| Check actions | Mark open, mark settled (refuses checks with money outstanding), duplicate a check with its items and shares |
| Check item form | Per-participant shares with weights |
| Receipt uploads | Photo thumbnail and preview, parsed line items, token usage, link to the created check, actions to re-read a photo or re-create its check |
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
| `ANTHROPIC_API_KEY` | unset | Credentials for reading receipts. The SDK also accepts `ANTHROPIC_AUTH_TOKEN` or an `ant auth login` profile |
| `RECEIPT_PARSER_MODEL` | `claude-opus-5` | Model used to read receipts |
| `RECEIPT_PARSER_TIMEOUT` | `120` | Seconds to wait on the API |
| `RECEIPT_PARSE_ON_UPLOAD` | `True` | Read a receipt as soon as it is uploaded |
| `RECEIPT_CREATE_CHECK_ON_PARSE` | `True` | Create the draft check straight after reading |

## Tests

```bash
python manage.py test
```

Covers the money arithmetic (rounding, largest-remainder allocation, discount
capping), the settlement rules, the receipt-to-check mapping, and the admin
screens, actions and filters.

No test calls the real API. The parsing tests run the genuine SDK request path
against a local stub of the Messages API, so the request shape — vision block,
JSON schema, downscaled image — is checked without spending a token.

## Project layout

```
checkcalc/        project settings, URLs, WSGI/ASGI entry points
checks/
  models.py       checks, items, shares, payments, receipt uploads
  parsing.py      image preparation and the Claude vision call
  importers.py    parsed receipt -> check, items and shares
  admin.py        the whole user interface
  management/commands/   seed_demo, parse_receipt
```

# check-calc

A small Django project for splitting shared checks — restaurant bills, group
orders, anything where several people chip in — driven entirely from the Django
admin. Data lives in a local SQLite database.

## What it does

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

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python manage.py migrate          # creates db.sqlite3
python manage.py seed_demo        # optional: demo participants and a check
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://127.0.0.1:8000/> — the root redirects straight to
`/admin/`.

## Admin features

| Screen | What you get |
| --- | --- |
| Check list | Status badge, item count, subtotal / total / paid / outstanding columns, date hierarchy, search, filters by status, date and settlement state |
| Check form | Line items and payments as inlines, a live totals panel and a "who owes what" settlement table |
| Check actions | Mark open, mark settled (refuses checks with money outstanding), duplicate a check with its items and shares |
| Check item form | Per-participant shares with weights |
| Participants | Checks shared in, total paid |
| Payments | Filter and search by participant, check, method and date |

The check list computes its money columns with correlated subqueries, so the
number of database queries does not grow with the number of rows — there is a
test that keeps it that way.

## Project layout

```
checkcalc/        project settings, URLs, WSGI/ASGI entry points
checks/           the app: models, admin, migrations, tests
  management/commands/seed_demo.py   demo data
manage.py
```

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

## Tests

```bash
python manage.py test
```

Covers the money arithmetic (rounding, largest-remainder allocation, discount
capping), the settlement rules, and the admin screens, actions and filters.

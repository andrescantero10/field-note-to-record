# Field Note to Record

A voice-first deal-to-document prototype for a small robotics distributor.
Built as the final project for CEN 4010 (Principles of Software Engineering, FAU).

> **Status:** scaffold only. Sections marked _TBD_ are filled in as each part is built.

## Purpose

A salesperson works only from his phone and does not use the CRM, so the owner
re-types his visit notes and quotes go out late. This prototype lets him submit
a short note (typed or dictated in the browser), review the CRM records extracted
from it, and confirm them before anything is written to Zoho CRM. A confirmed deal
can then be turned into a draft quote whose figures come only from deterministic
pricing code, never from the model.

## Architecture

_TBD._ See `docs/architecture.md` once written.

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # defaults run fully offline: fake model, mock Zoho
```

## Run

_TBD._

## Test

```bash
pytest
```

## Sample data

All customers and products in this repo are invented.

**Product catalog** (`data/catalog.csv`): one row per dated price entry, with
columns `id, name, unit_price, active, effective_date`. The price in force on a
date is the row with the latest `effective_date` on or before it; an inactive
row means the product is discontinued from that date. A product with no current
price cannot be quoted. The sample includes a past price change (`ARM-12KG`),
a future one (`ARM-6KG`), a product not yet available (`VIS-3D`) and a
discontinued one (`GRIP-2F-50`).

## Known limitations

- Audit log: the hash chain detects edited or deleted entries in the middle of
  the log, but not removal of the most recent entries (the shortened chain is
  still valid). Detecting that would need the latest hash stored outside the database.
- Quotes are pre-tax. Sales tax (rates, exemptions) is out of scope.
- Catalog: loaded from a CSV file in the repo. Syncing it from the Zoho CRM
  Products module is planned with the Zoho adapter; Zoho has no price
  effective-date field by default, so that needs a custom field.

## Docs

- `docs/AI_ASSISTANCE.md`: what AI tools generated, and what was rejected or changed
- `docs/CHANGES.md`: scope changes from the original design report

## License

MIT. See `LICENSE`.

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

The web app is not built yet. To try one extraction from the command line:

```bash
.venv/bin/python scripts/try_extraction.py
```

With the default settings this uses the fake model, which returns an empty
answer. To use Claude, set `LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY` in
`.env`. That makes a real, paid API call and sends the note's text to Anthropic,
so use invented notes only.

## Test

```bash
pytest
```

## Sample data

All customers and products in this repo are invented.

**Product catalog** (`data/catalog.csv`): one row per dated price entry, with
columns `id, name, unit_price, tax_rate, active, effective_date`. `tax_rate` is a
percentage (`0` = not taxed). The row in force on a date is the one with the
latest `effective_date` on or before it; an inactive row means the product is
discontinued from that date. A product with no current price cannot be quoted.
The sample includes a past price change (`ARM-12KG`), a future one (`ARM-6KG`),
a product not yet available (`VIS-3D`), a discontinued one (`GRIP-2F-50`), and
untaxed services (`SVC-*`). Tax rates are invented.

**Sample notes** (`data/sample_notes/`): two ordinary visit notes and eight
hostile ones (planted instructions, a fake owner approval, a script tag, text
that tries to break the answer format, a look-alike product code, database
commands). `tests/test_injection.py` runs every note against a model that
returns the worst possible answer.

**Pricing rules** are written in [`docs/PRICING_SPEC.md`](docs/PRICING_SPEC.md):
tax per line, subtotal including tax, and a person-entered dollar discount taken
off after tax.

## Known limitations

- Audit log: the hash chain detects edited or deleted entries in the middle of
  the log, but not removal of the most recent entries (the shortened chain is
  still valid). Detecting that would need the latest hash stored outside the database.
- Model: with `LLM_PROVIDER=claude`, the text of each note is sent to Anthropic's
  API. The prototype has no consent step or redaction for that, so only invented
  notes should be used. The provider has been tried on one example note, not
  measured on a test set.
- A false statement in a note is still shown to the reviewer as text from the
  note (for example "the owner already approved this"). The app cannot judge
  whether it is true. It never treats such text as an approval: approvals only
  come from the named approver acting in the app.
- The injection tests cover the note, extraction, candidate and pricing steps.
  The CRM write and quote approval steps are not built yet and so not yet attacked.
- Pricing: the tax rate comes from the product's catalog row, not from the
  delivery address. No per-line discounts, no "adjustment" amount, purchase
  quotes only (no rental or leasing). Tax rounding is assumed to be half-up per
  line, matching observed Zoho quotes; not yet verified against a Zoho test org.
- Catalog: loaded from a CSV file in the repo. Syncing it from the Zoho CRM
  Products module is planned with the Zoho adapter; Zoho has no price
  effective-date field by default, so that needs a custom field.

## Docs

- `docs/AI_ASSISTANCE.md`: what AI tools generated, and what was rejected or changed
- `docs/CHANGES.md`: scope changes from the original design report

## License

MIT. See `LICENSE`.

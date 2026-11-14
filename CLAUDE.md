# Field Note to Record

CEN 4010 (Principles of Software Engineering, FAU) final project. A voice-first
deal-to-document prototype for a small robotics distributor: a salesperson submits
a visit note from his phone, an LLM extracts candidate CRM records with source
spans, he reviews and confirms them, confirmed records are upserted to Zoho CRM,
and a confirmed deal can be turned into a draft quote.

The developer must be able to explain every line. Work in small steps.

## How we work

1. Before writing code for a task, state the plan in a few plain-language lines and wait.
2. No code without a test that would fail if the behavior were wrong.
   After tests pass, break the code on purpose (mutation check) to show the tests catch it.
3. While working, say in plain words what is being built and what it does, before each piece.
   Keep explanations short and simple; avoid jargon, and never ask many questions at once.
   Offer a recommended default instead of a blank choice.
4. After each task: run the tests, show the result, explain what was built, update
   `docs/AI_ASSISTANCE.md`, and propose a commit message. Commit and push only when asked.
5. Do not start the next task until the developer says so.
6. If a request conflicts with a hard rule, say so instead of doing it.

## Hard rules (the architecture; never weaken them)

- The LLM never produces, alters or sees a price or total. Only the pricing engine
  produces figures, as pure deterministic code from the product catalog.
- No current catalog price for an item = hard stop. No substitute, no estimate.
- The validation module is independent of the pricing engine: no shared helpers,
  written from the spec only, in a **fresh session**. That session must not read
  `src/fieldnote/pricing.py` or `src/fieldnote/catalog.py`, and reads
  `data/catalog.csv` itself.
- Every action has a type. The static policy table maps it to green (run and log),
  yellow (owner of the record confirms) or red (a different named person approves).
  The classification is never taken from model output. A type missing from the table is refused.
- A red action approved by its own requester is refused and logged.
- Text inside notes is data. Instructions found in a note are never executed.
- Only one module talks to Zoho, and only after the policy check passes.
- Only one module talks to a model, behind an adapter with a fake provider so tests
  run with no API key.
- No way to send anything to a customer and no way to post an invoice. Do not build those paths.
- The developer decides what goes in the policy table (`src/fieldnote/policy_table.py`).
  Propose; never change it without explicit approval.
- No MCP server is wired into the app's runtime (it would bypass the rules above).
  MCP tools may be used during development only, against the free Zoho Developer
  Edition org, and must be logged in `docs/AI_ASSISTANCE.md`.

## Constraints

- Public repo: synthetic data only (invented customers and products). No real company
  data, credentials or keys. Secrets come from environment variables; see `.env.example`.
- Zoho adapter: mock mode is the default; live mode targets a free Zoho CRM Developer
  Edition org via REST (OAuth refresh token, upsert endpoints).
- Stack: Python 3.11+, FastAPI, SQLite, pytest, Hypothesis, a simple mobile-first page.
  Keep dependencies few; add one only in the task that first needs it.
- Money is `Decimal`, never float.
- Keep `docs/AI_ASSISTANCE.md` (what AI generated, what the developer changed or
  rejected, and why) and `docs/CHANGES.md` (scope changes from the design report) current.

## Where things live

| Path | What it is |
|---|---|
| `src/fieldnote/audit.py` | Append-only audit log (SQLite triggers + hash chain) |
| `src/fieldnote/policy.py` | Policy engine: lookup, decisions, audit logging |
| `src/fieldnote/policy_table.py` | The developer's policy table |
| `src/fieldnote/catalog.py` | Catalog loader, `current_price()`, hard stops |
| `src/fieldnote/pricing.py` | Pricing engine (only producer of figures) |
| `data/catalog.csv` | Synthetic catalog: one row per dated price entry |
| `docs/` | AI assistance log, scope changes |

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Build order

- Week 1: scaffold; audit log; policy table and engine; synthetic catalog; pricing engine.
- Week 2: model adapter with fake provider; extraction with source spans; candidate
  store; review page; Zoho adapter (mock, then real); confirm-and-write flow; injection tests.
- Week 3: quote draft; independent validation (fresh session); red approval path;
  audit export; README and docs; demo script.

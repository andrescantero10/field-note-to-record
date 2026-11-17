"""Run one extraction and print the result. A manual check, not a test.

    .venv/bin/python scripts/try_extraction.py
    .venv/bin/python scripts/try_extraction.py "Met Sam at Northgate Foods. They want 3 ARM-6KG."

It uses the provider named by LLM_PROVIDER in .env. With LLM_PROVIDER=claude
this makes one real, paid API call and sends the note's text to Anthropic, so
use invented notes only. The audit log goes to a temporary file.
"""

import sys
import tempfile
from pathlib import Path

from fieldnote.audit import AuditLog
from fieldnote.catalog import Catalog
from fieldnote.config import load_settings
from fieldnote.extraction import ExtractionFailed, extract
from fieldnote.llm import get_provider
from fieldnote.policy import PolicyEngine
from fieldnote.policy_table import POLICY_TABLE

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_NOTE = (
    "Met Dana at Bayside Packaging. They want two 6 kg arms and a gripper. "
    "I told her we could do 5% off if they sign this month. Call her Friday."
)


def show(label: str, value: str, flags: tuple[str, ...]) -> None:
    print(f"  {label:<15} {value}")
    for flag in flags:
        print(f"  {'':<15} check: {flag}")


def main() -> int:
    note = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_NOTE
    settings = load_settings(ROOT / ".env")
    provider = get_provider(settings)
    catalog = Catalog.from_csv(ROOT / "data" / "catalog.csv")

    with tempfile.TemporaryDirectory() as folder:
        with AuditLog(Path(folder) / "audit.db") as audit:
            policy = PolicyEngine(audit, POLICY_TABLE)
            try:
                result = extract(note, "sales_rep", provider=provider, catalog=catalog,
                                 policy=policy, audit=audit)
            except ExtractionFailed as error:
                print(f"Extraction failed: {error}")
                if error.__cause__ is not None:
                    print(f"  because: {error.__cause__}")
                return 1
            answered_by = audit.events()[-1].details

    print(f"Note: {note}\n")
    print(f"Answered by: {answered_by['provider']} / {answered_by['model']}\n")
    print("Fields")
    for f in result.fields:
        if f.value is None:
            show(f.name, "(not stated)", ())
        else:
            quote = note[f.span[0] : f.span[1]]
            show(f.name, f"{f.value!r}  [{f.kind}]  from {quote!r}", f.reasons)
    print("\nDeal items")
    for item in result.items:
        product = item.product_id or "(no product matched)"
        quote = note[item.span[0] : item.span[1]]
        show(product, f"x {item.quantity}  [{item.kind}]  from {quote!r}", item.reasons)
    print("\nCommitments")
    for c in result.commitments:
        show("promise", repr(c.value), c.reasons)
    print("\nDropped (returned by the model but not accepted)")
    for d in result.dropped:
        show(d.where, d.reason, ())
    if not result.dropped:
        print("  nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

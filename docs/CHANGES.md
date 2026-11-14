# Scope Changes from the Design Report

Changes between the original design report and what this prototype builds.

| Cut from design report     | What the prototype does instead                          | Reason |
|----------------------------|----------------------------------------------------------|--------|
| Native offline app         | Mobile-first web page (typed or browser dictation)       |        |
| Audio timestamps           | Character spans in the note text for every field         |        |
| Invoice posting            | No path to post an invoice exists in the code            |        |
| Reconciliation queue       | Not built                                                |        |
| SSO                        | Not built                                                |        |

## Changes made during the build

| Date | Change | Reason |
|---|---|---|
| 2026-11-14 | Quotes now include sales tax per line (rate from the catalog row; 0 = untaxed). The subtotal includes tax. | A read-only review of the distributor's real CRM showed every quote carries line-level tax. Pre-tax quotes would not match what the business sends. |
| 2026-11-14 | Discount changed from a percentage to a dollar amount, applied once after tax, never larger than the subtotal. Any discount is still Red. | The same review showed discounts are entered as flat dollar amounts on the quote. |
| 2026-11-14 | Pricing rules written down in `docs/PRICING_SPEC.md`. | The independent validation module must be written from a spec, without reading the pricing code. |

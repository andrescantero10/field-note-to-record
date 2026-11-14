# Pricing Specification

This document is the single statement of how a quote's figures are computed.
The pricing engine implements it, and the independent validation module checks
quotes against it. **The validation module must be written from this document
alone**, without reading the pricing engine's or catalog loader's code.

The rules follow how the distributor quotes today in Zoho CRM: tax per line,
subtotal including tax, and a flat dollar discount taken off after tax.

## 1. Money

- All amounts are exact decimal numbers in US dollars. Never use floating point.
- Every amount on a quote has exactly two decimals (whole cents).
- The only rounding step is the tax on each line (section 4). Nothing else is rounded.
- Rounding is **half-up** to the cent: 0.065 becomes 0.07, 0.325 becomes 0.33.
  (Banker's rounding, which would give 0.06 and 0.32, is wrong here.)

## 2. The catalog

The catalog is the file `data/catalog.csv`. Its columns are exactly:

| Column | Meaning |
|---|---|
| `id` | Product code. Matched exactly: case and spaces matter. |
| `name` | Product name. |
| `unit_price` | Price per unit in dollars: digits, optionally a dot and 1 or 2 decimals. Greater than 0. |
| `tax_rate` | Tax as a percentage, same number format. `6.50` means 6.5%. `0` means not taxed. Below 100. |
| `active` | `true` or `false` (any letter case). |
| `effective_date` | `YYYY-MM-DD`. The date this row starts to apply. |

A product can have several rows with different `effective_date`s (a price or tax
change is a new row). Two rows for the same product cannot share a date.

**The row that applies on a quote date** is the row for that product with the
latest `effective_date` on or before the quote date.

## 3. Hard stops (no quote is produced)

A quote is refused as a whole, with no partial result, if any line has:

1. a product id that is not in the catalog;
2. no row on or before the quote date (the product is not available yet);
3. an applicable row with `active = false` (the product is discontinued). An older
   active row must **not** be used instead.

A quote is also refused if it has no lines, if any quantity is not a whole number
of at least 1, or if the discount breaks the rules in section 6.

There are no substitutes and no estimates. A price never comes from anywhere but
the catalog row that applies.

## 4. Each line

For each requested line (product, quantity):

```
unit_price  = unit_price from the applicable catalog row
tax_rate    = tax_rate from the same row
amount      = unit_price × quantity                          (exact)
tax         = amount × tax_rate ÷ 100, rounded half-up to the cent
line_total  = amount + tax
```

Lines are kept in the order requested. The same product may appear on more than
one line; each line is computed on its own.

## 5. Quote totals

```
total_before_tax = sum of all line amounts
total_tax        = sum of all line taxes
subtotal         = sum of all line totals      (= total_before_tax + total_tax)
grand_total      = subtotal − discount
```

Changing the order of the lines never changes any total.

## 6. Discount

- The discount is a dollar amount typed by a person when drafting the quote.
  It is never taken from the AI's reading of a note.
- It is applied once, to the whole quote, **after tax**: it reduces the grand total,
  not the tax.
- It must be at least 0.00, have at most two decimals, and be no larger than the
  subtotal. Anything else is refused.
- **Any discount above 0.00 makes the quote Red**: it needs approval from the
  named approver, who cannot be the person who requested it.

## 7. The quote record

A priced quote carries these fields:

| Field | Content |
|---|---|
| `priced_on` | The quote date used for the catalog lookup |
| `lines` | For each line: `product_id`, `name`, `quantity`, `unit_price`, `tax_rate`, `price_effective_date` (the `effective_date` of the row used), `amount`, `tax`, `line_total` |
| `total_before_tax`, `total_tax`, `subtotal`, `discount`, `grand_total` | As in sections 5 and 6 |
| `has_discount` | True when `discount` is above 0.00 |

## 8. Worked examples

All examples use `data/catalog.csv` with quote date **2026-11-14** unless stated.

### Example A: mixed taxed and untaxed lines, with a discount

| Line | Qty | Unit price | Tax rate | Amount | Tax | Line total |
|---|---|---|---|---|---|---|
| ARM-6KG | 2 | 18,450.00 | 6.50 | 36,900.00 | 2,398.50 | 39,298.50 |
| WAR-ARM-2Y | 1 | 3,600.00 | 6.50 | 3,600.00 | 234.00 | 3,834.00 |
| SVC-SHIP | 1 | 2,450.00 | 0 | 2,450.00 | 0.00 | 2,450.00 |

- total_before_tax = 42,950.00
- total_tax = 2,632.50
- subtotal = 45,582.50
- discount = 2,000.00 (so the quote is Red)
- grand_total = 43,582.50

### Example B: tax that needs rounding

1 × GRIP-VAC-4 at 1,875.50, tax 6.50%: 1,875.50 × 6.5 ÷ 100 = 121.9075, so the tax is
**121.91** and the line total is **1,997.41**.

### Example C: an exact half cent

A product at 5.00 with tax 6.50%: 5.00 × 6.5 ÷ 100 = 0.325, so the tax is **0.33**
(half-up), not 0.32.

### Example D: the date picks the row

ARM-6KG costs 18,450.00 on 2026-12-31 and 19,200.00 on 2027-01-01: the new row
applies on its own date.

### Example E: refusals

- VIS-3D on 2026-11-14: refused, not available until 2027-02-01.
- GRIP-2F-50 on 2026-10-01: refused, discontinued on 2026-09-01. The old price of
  2,480.00 is not used.
- 1 × MNT-PED (subtotal 660.30) with a discount of 700.00: refused, the discount is
  larger than the subtotal.

## 9. Not covered

- Discounts on individual lines (Zoho supports them; past quotes did not use them).
- Rental and leasing quotes, recurring items. Purchase quotes only.
- Tax by delivery location. As in Zoho, the rate comes from the product row.
- The "Adjustment" field Zoho offers on quotes.

"""Pricing engine: the only code in the app that produces prices and totals.

The rules are written out in docs/PRICING_SPEC.md; this module implements
them. price_quote() is a pure function. It reads unit prices and tax rates
from the catalog and nothing else: no model, no network, no clock (the date
is passed in), no randomness. The same inputs always give the same quote.

Per line:
    amount      = unit_price x quantity                         (exact)
    tax         = amount x tax_rate / 100, rounded half-up to the cent
    line_total  = amount + tax

Per quote:
    total_before_tax = sum of amounts
    total_tax        = sum of line taxes
    subtotal         = sum of line totals  (includes tax, as in Zoho CRM)
    discount         = a dollar amount entered by a person, taken off after tax
    grand_total      = subtotal - discount

If any line has no current catalog price, NoCurrentPrice is raised and no
quote is produced at all: no skipped lines, no estimates, no substitutes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fieldnote.catalog import Catalog

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class QuoteLine:
    """One requested line: which product and how many."""

    product_id: str
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.product_id, str) or not self.product_id.strip():
            raise ValueError("product_id must be a non-blank string")
        # bool is a subclass of int in Python, so True would otherwise count as 1.
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ValueError(f"quantity must be a whole number, got {self.quantity!r}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be at least 1, got {self.quantity}")


@dataclass(frozen=True)
class PricedLine:
    product_id: str
    name: str
    quantity: int
    unit_price: Decimal
    tax_rate: Decimal
    price_effective_date: date  # which catalog entry was used, for traceability
    amount: Decimal
    tax: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class PricedQuote:
    priced_on: date
    lines: tuple[PricedLine, ...]
    total_before_tax: Decimal
    total_tax: Decimal
    subtotal: Decimal
    discount: Decimal
    grand_total: Decimal

    @property
    def has_discount(self) -> bool:
        """True for any discount above $0. The quote step uses this to choose
        quote.release_discounted (red) instead of quote.release."""
        return self.discount > 0


def _check_discount_format(discount: Decimal) -> Decimal:
    # Floats are refused outright: 0.1 as a float is 0.1000000000000000055...
    if not isinstance(discount, Decimal):
        raise TypeError("discount must be a Decimal, e.g. Decimal('500')")
    if not discount.is_finite():
        raise ValueError("discount must be a number")
    if discount < 0:
        raise ValueError(f"discount cannot be negative, got {discount}")
    if discount != discount.quantize(CENT):
        raise ValueError(f"discount can have at most 2 decimals, got {discount}")
    return discount.quantize(CENT)


def _line_tax(amount: Decimal, tax_rate: Decimal) -> Decimal:
    # The only rounding step in the engine, done once per line. ROUND_HALF_UP
    # is the everyday rule ($0.065 -> $0.07); Python's default would round
    # half to even ($0.065 -> $0.06).
    return (amount * tax_rate / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def price_quote(
    catalog: Catalog,
    lines: Sequence[QuoteLine],
    on_date: date,
    discount: Decimal = Decimal("0"),
) -> PricedQuote:
    """Price every line from the catalog as of on_date and total the quote.

    Raises NoCurrentPrice (from the catalog) if any product has no current
    price, and ValueError if the discount is larger than the subtotal.
    Nothing is returned in either case.
    """
    if not lines:
        raise ValueError("a quote needs at least one line")
    for line in lines:
        if not isinstance(line, QuoteLine):
            raise TypeError(f"each line must be a QuoteLine, got {line!r}")
    discount = _check_discount_format(discount)

    priced_lines = []
    for line in lines:
        entry = catalog.current_price(line.product_id, on_date)  # hard stop if none
        amount = entry.unit_price * line.quantity
        tax = _line_tax(amount, entry.tax_rate)
        priced_lines.append(
            PricedLine(
                product_id=entry.product_id,
                name=entry.name,
                quantity=line.quantity,
                unit_price=entry.unit_price,
                tax_rate=entry.tax_rate,
                price_effective_date=entry.effective_date,
                amount=amount,
                tax=tax,
                line_total=amount + tax,
            )
        )

    total_before_tax = sum((p.amount for p in priced_lines), ZERO)
    total_tax = sum((p.tax for p in priced_lines), ZERO)
    subtotal = sum((p.line_total for p in priced_lines), ZERO)
    if discount > subtotal:
        raise ValueError(f"discount {discount} is larger than the subtotal {subtotal}")

    return PricedQuote(
        priced_on=on_date,
        lines=tuple(priced_lines),
        total_before_tax=total_before_tax,
        total_tax=total_tax,
        subtotal=subtotal,
        discount=discount,
        grand_total=subtotal - discount,
    )

"""Pricing engine: the only code in the app that produces prices and totals.

price_quote() is a pure function. It reads unit prices from the catalog and
nothing else: no model, no network, no clock (the date is passed in), no
randomness. The same inputs always give the same quote.

    line_total      = unit_price x quantity              (exact)
    subtotal        = sum of line totals                 (exact)
    discount_amount = subtotal x discount% / 100, rounded half-up to the cent
    total           = subtotal - discount_amount

If any line has no current catalog price, NoCurrentPrice is raised and no
quote is produced at all: no skipped lines, no estimates, no substitutes.

The discount percentage is entered by a person, never taken from a model.
Totals are pre-tax.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fieldnote.catalog import Catalog

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


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
    price_effective_date: date  # which catalog entry was used, for traceability
    line_total: Decimal


@dataclass(frozen=True)
class PricedQuote:
    priced_on: date
    lines: tuple[PricedLine, ...]
    subtotal: Decimal
    discount_percent: Decimal
    discount_amount: Decimal
    total: Decimal

    @property
    def has_discount(self) -> bool:
        """True for any discount above 0%. The quote step uses this to choose
        quote.release_discounted (red) instead of quote.release."""
        return self.discount_percent > 0


def _check_discount(discount_percent: Decimal) -> Decimal:
    # Floats are refused outright: 5.1 as a float is 5.0999999999999996447...
    if not isinstance(discount_percent, Decimal):
        raise TypeError("discount_percent must be a Decimal, e.g. Decimal('5')")
    if not discount_percent.is_finite():
        raise ValueError("discount_percent must be a number")
    if not Decimal("0") <= discount_percent < HUNDRED:
        raise ValueError(f"discount_percent must be from 0 up to (not including) 100, got {discount_percent}")
    if discount_percent != discount_percent.quantize(CENT):
        raise ValueError(f"discount_percent can have at most 2 decimals, got {discount_percent}")
    return discount_percent.quantize(CENT)


def price_quote(
    catalog: Catalog,
    lines: Sequence[QuoteLine],
    on_date: date,
    discount_percent: Decimal = Decimal("0"),
) -> PricedQuote:
    """Price every line from the catalog as of on_date and total the quote.

    Raises NoCurrentPrice (from the catalog) if any product has no current
    price; nothing is returned in that case.
    """
    if not lines:
        raise ValueError("a quote needs at least one line")
    for line in lines:
        if not isinstance(line, QuoteLine):
            raise TypeError(f"each line must be a QuoteLine, got {line!r}")
    discount_percent = _check_discount(discount_percent)

    priced_lines = []
    for line in lines:
        entry = catalog.current_price(line.product_id, on_date)  # hard stop if none
        priced_lines.append(
            PricedLine(
                product_id=entry.product_id,
                name=entry.name,
                quantity=line.quantity,
                unit_price=entry.unit_price,
                price_effective_date=entry.effective_date,
                line_total=entry.unit_price * line.quantity,
            )
        )

    subtotal = sum((p.line_total for p in priced_lines), Decimal("0.00"))
    # The only rounding step. ROUND_HALF_UP is the everyday rule (1.145 -> 1.15);
    # Python's default would round half to even (1.145 -> 1.14).
    discount_amount = (subtotal * discount_percent / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
    total = subtotal - discount_amount

    return PricedQuote(
        priced_on=on_date,
        lines=tuple(priced_lines),
        subtotal=subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
    )

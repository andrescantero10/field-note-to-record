"""Product catalog: loads price entries from a CSV file and answers one
question, "what is the current price of product X on date D?".

Each row is a dated price entry, so a product can have several rows:

    id,name,unit_price,tax_rate,active,effective_date
    ARM-12KG,...,25900.00,6.50,true,2026-01-01
    ARM-12KG,...,26900.00,6.50,true,2026-07-01   <- new price from July 1
    SVC-SHIP,...,2450.00,0,true,2026-01-01       <- not taxed

tax_rate is a percentage (6.50 means 6.5%); 0 means the item is not taxed.
A tax change is made the same way as a price change: a new dated row.

The entry that applies on a date is the one with the latest effective_date
on or before that date. If that entry is inactive, the product is
discontinued from that date. There is no fallback to an older price and no
substitute product: if no current price exists, NoCurrentPrice is raised.

Prices are Decimal, never float, so totals are exact.
"""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

COLUMNS = ("id", "name", "unit_price", "tax_rate", "active", "effective_date")

# Digits, optionally a dot and one or two decimals. Rejects "-5", "1,200",
# "$100", "1e3", "12.345", "7%", "NaN" before Decimal ever sees them.
# Used for both unit_price and tax_rate.
_AMOUNT_PATTERN = re.compile(r"\d+(\.\d{1,2})?")
_HUNDRED = Decimal("100")
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_BOOLEANS = {"true": True, "false": False}  # compared lowercased, so Excel's TRUE works


class CatalogError(ValueError):
    """The catalog file is malformed. Raised at load time, never at quote time."""


class NoCurrentPrice(LookupError):
    """Hard stop: there is no current price for this product on this date."""

    def __init__(self, product_id: str, on_date: date, reason: str) -> None:
        super().__init__(f"no current price for {product_id!r} on {on_date}: {reason}")
        self.product_id = product_id
        self.on_date = on_date
        self.reason = reason


@dataclass(frozen=True)
class PriceEntry:
    product_id: str
    name: str
    unit_price: Decimal
    tax_rate: Decimal  # percent; 0 = not taxed
    active: bool
    effective_date: date


def _parse_row(row: dict[str, str], line: int) -> PriceEntry:
    def fail(message: str) -> CatalogError:
        return CatalogError(f"line {line}: {message}")

    # csv.DictReader puts cells beyond the header under the key None.
    if None in row:
        raise fail("more cells than columns (is there an unquoted comma in the name?)")
    values = {column: (row[column] or "").strip() for column in COLUMNS}

    if not values["id"]:
        raise fail("id is blank")
    if not values["name"]:
        raise fail("name is blank")

    price_text = values["unit_price"]
    if not _AMOUNT_PATTERN.fullmatch(price_text):
        raise fail(f"unit_price {price_text!r} is not a plain amount like 1200 or 1200.50")
    unit_price = Decimal(price_text).quantize(Decimal("0.01"))
    if unit_price <= 0:
        raise fail("unit_price must be greater than zero")

    tax_text = values["tax_rate"]
    if not _AMOUNT_PATTERN.fullmatch(tax_text):
        raise fail(f"tax_rate {tax_text!r} is not a plain percentage like 0, 6.5 or 10.25")
    tax_rate = Decimal(tax_text).quantize(Decimal("0.01"))
    if tax_rate >= _HUNDRED:
        raise fail("tax_rate must be below 100")

    active_text = values["active"].lower()
    if active_text not in _BOOLEANS:
        raise fail(f"active must be true or false, got {values['active']!r}")

    date_text = values["effective_date"]
    if not _DATE_PATTERN.fullmatch(date_text):
        raise fail(f"effective_date {date_text!r} must be YYYY-MM-DD")
    try:
        effective_date = date.fromisoformat(date_text)
    except ValueError:
        raise fail(f"effective_date {date_text!r} is not a real date") from None

    return PriceEntry(
        product_id=values["id"],
        name=values["name"],
        unit_price=unit_price,
        tax_rate=tax_rate,
        active=_BOOLEANS[active_text],
        effective_date=effective_date,
    )


class Catalog:
    def __init__(self, entries: Iterable[PriceEntry]) -> None:
        by_product: dict[str, list[PriceEntry]] = {}
        for entry in entries:
            by_product.setdefault(entry.product_id, []).append(entry)

        for product_id, product_entries in by_product.items():
            dates = [e.effective_date for e in product_entries]
            if len(dates) != len(set(dates)):
                raise CatalogError(f"{product_id}: two entries with the same effective_date")
            # Newest first, so current_price can stop at the first match.
            product_entries.sort(key=lambda e: e.effective_date, reverse=True)

        if not by_product:
            raise CatalogError("catalog is empty")
        self._by_product = by_product

    @classmethod
    def from_csv(cls, path: str | os.PathLike[str]) -> Catalog:
        # utf-8-sig skips the invisible marker Excel puts at the start of CSV files.
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or set(reader.fieldnames) != set(COLUMNS):
                raise CatalogError(
                    f"columns must be exactly {', '.join(COLUMNS)}; got {reader.fieldnames}"
                )
            # Line 1 is the header, so the first data row is line 2.
            entries = [_parse_row(row, line) for line, row in enumerate(reader, start=2)]
        return cls(entries)

    def product_names(self) -> dict[str, str]:
        """Every product id with its name, and nothing else. This is the only
        catalog information the model is shown: no prices, no tax rates.
        Discontinued and future products are included, so a note that asks for
        one is matched and then stopped by current_price() with a clear reason."""
        # Entries are sorted newest first, so [0] carries the latest name.
        return {product_id: entries[0].name for product_id, entries in self._by_product.items()}

    def current_price(self, product_id: str, on_date: date) -> PriceEntry:
        """Return the entry in force on on_date, or raise NoCurrentPrice."""
        # datetime is a subclass of date; comparing the two raises, so refuse it up front.
        if isinstance(on_date, datetime) or not isinstance(on_date, date):
            raise TypeError("on_date must be a date, not a datetime")

        entries = self._by_product.get(product_id)  # exact match only
        if entries is None:
            raise NoCurrentPrice(product_id, on_date, "unknown product")

        for entry in entries:  # newest first
            if entry.effective_date <= on_date:
                if not entry.active:
                    raise NoCurrentPrice(product_id, on_date, "inactive")
                return entry

        raise NoCurrentPrice(product_id, on_date, "not yet effective")

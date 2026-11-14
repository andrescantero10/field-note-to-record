"""Tests for the pricing engine.

Part 1: hand-worked examples against the sample catalog.
Part 2: property-based tests. Hypothesis generates random catalogs and
quotes and checks rules that must hold for every one of them. When a rule
fails, Hypothesis shrinks the input to the smallest failing example.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fieldnote.catalog import Catalog, NoCurrentPrice, PriceEntry
from fieldnote.pricing import QuoteLine, price_quote

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "catalog.csv"
TODAY = date(2026, 11, 14)


@pytest.fixture(scope="module")
def catalog():
    return Catalog.from_csv(SAMPLE)


# =============================================================================
# Part 1: hand-worked examples
# =============================================================================


def test_worked_example_with_discount(catalog):
    # 2 x ARM-6KG    @ 18,450.00 = 36,900.00
    # 1 x GRIP-2F-85 @  3,150.00 =  3,150.00
    # subtotal                     40,050.00
    # 5% discount                  -2,002.50
    # total                        38,047.50
    quote = price_quote(
        catalog,
        [QuoteLine("ARM-6KG", 2), QuoteLine("GRIP-2F-85", 1)],
        TODAY,
        Decimal("5"),
    )
    assert [line.line_total for line in quote.lines] == [Decimal("36900.00"), Decimal("3150.00")]
    assert quote.subtotal == Decimal("40050.00")
    assert quote.discount_amount == Decimal("2002.50")
    assert quote.total == Decimal("38047.50")
    assert quote.has_discount


def test_no_discount_quote(catalog):
    quote = price_quote(catalog, [QuoteLine("SVC-INSTALL", 3)], TODAY)
    assert quote.total == quote.subtotal == Decimal("4350.00")
    assert quote.discount_amount == Decimal("0.00")
    assert not quote.has_discount


def test_rounding_is_half_up_not_half_even(catalog):
    # 2,290.00 x 0.05% = 1.145 exactly. Half-up gives 1.15; Python's default
    # (half-even) would give 1.14.
    quote = price_quote(catalog, [QuoteLine("VIS-2D", 1)], TODAY, Decimal("0.05"))
    assert quote.discount_amount == Decimal("1.15")
    assert quote.total == Decimal("2288.85")


def test_price_depends_on_quote_date(catalog):
    before = price_quote(catalog, [QuoteLine("ARM-6KG", 1)], date(2026, 12, 31))
    after = price_quote(catalog, [QuoteLine("ARM-6KG", 1)], date(2027, 1, 1))
    assert before.total == Decimal("18450.00")
    assert after.total == Decimal("19200.00")
    assert after.lines[0].price_effective_date == date(2027, 1, 1)


@pytest.mark.parametrize(
    "product_id, reason",
    [("GRIP-2F-50", "inactive"), ("VIS-3D", "not yet effective"), ("NOPE-1", "unknown product")],
)
def test_one_unpriceable_line_stops_the_whole_quote(catalog, product_id, reason):
    lines = [QuoteLine("ARM-6KG", 1), QuoteLine(product_id, 1), QuoteLine("MNT-PED", 2)]
    with pytest.raises(NoCurrentPrice) as stop:
        price_quote(catalog, lines, TODAY)
    assert stop.value.reason == reason


# --- Input checks --------------------------------------------------------------


@pytest.mark.parametrize("quantity", [0, -1, 2.5, True, "2", None])
def test_bad_quantity_is_refused(quantity):
    with pytest.raises(ValueError, match="quantity"):
        QuoteLine("ARM-6KG", quantity)


@pytest.mark.parametrize("product_id", ["", "   ", None])
def test_blank_product_id_is_refused(product_id):
    with pytest.raises(ValueError, match="product_id"):
        QuoteLine(product_id, 1)


def test_float_discount_is_refused(catalog):
    with pytest.raises(TypeError, match="Decimal"):
        price_quote(catalog, [QuoteLine("ARM-6KG", 1)], TODAY, 5.0)


@pytest.mark.parametrize("discount", ["-1", "100", "150", "5.125", "NaN", "Infinity"])
def test_out_of_range_discount_is_refused(catalog, discount):
    with pytest.raises(ValueError, match="discount_percent"):
        price_quote(catalog, [QuoteLine("ARM-6KG", 1)], TODAY, Decimal(discount))


def test_empty_quote_is_refused(catalog):
    with pytest.raises(ValueError, match="at least one line"):
        price_quote(catalog, [], TODAY)


def test_plain_tuple_instead_of_quote_line_is_refused(catalog):
    with pytest.raises(TypeError, match="QuoteLine"):
        price_quote(catalog, [("ARM-6KG", 1)], TODAY)


# =============================================================================
# Part 2: property-based tests
# =============================================================================

PRICE_DATE = date(2026, 1, 1)

# Prices from $0.01 to $100,000.00, built from whole cents so they always
# have exactly two decimals, like the catalog loader produces.
prices = st.integers(min_value=1, max_value=10_000_000).map(lambda c: Decimal(c).scaleb(-2))

# 0% or anything from 0.01% to 99.99%.
discounts = st.one_of(
    st.just(Decimal("0")),
    st.integers(min_value=1, max_value=9_999).map(lambda c: Decimal(c).scaleb(-2)),
)


@st.composite
def catalogs_and_lines(draw):
    """A random catalog of 1-8 products and a random quote of 1-10 lines from it."""
    product_ids = [f"P{i}" for i in range(draw(st.integers(1, 8)))]
    entries = [
        PriceEntry(pid, f"Product {pid}", draw(prices), True, PRICE_DATE) for pid in product_ids
    ]
    lines = draw(
        st.lists(
            st.builds(QuoteLine, st.sampled_from(product_ids), st.integers(1, 1_000)),
            min_size=1,
            max_size=10,
        )
    )
    return Catalog(entries), lines


def is_cents(amount):
    return amount.as_tuple().exponent == -2


@given(catalogs_and_lines(), discounts)
def test_totals_always_add_up(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    for line in quote.lines:
        assert line.line_total == line.unit_price * line.quantity
    assert quote.subtotal == sum(line.line_total for line in quote.lines)
    assert quote.total == quote.subtotal - quote.discount_amount


@given(catalogs_and_lines(), discounts)
def test_every_unit_price_is_exactly_the_catalog_price(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    assert len(quote.lines) == len(lines)
    for requested, priced in zip(lines, quote.lines):
        assert priced.product_id == requested.product_id
        assert priced.quantity == requested.quantity
        assert priced.unit_price == catalog.current_price(requested.product_id, PRICE_DATE).unit_price


@given(catalogs_and_lines(), discounts)
def test_amounts_are_whole_cents_and_never_negative(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    amounts = [quote.subtotal, quote.discount_amount, quote.total]
    amounts += [line.line_total for line in quote.lines]
    for amount in amounts:
        assert is_cents(amount)
        assert amount >= 0
    assert quote.total <= quote.subtotal


@given(catalogs_and_lines(), discounts)
def test_discount_is_within_half_a_cent_of_exact(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    exact = quote.subtotal * discount / 100
    assert abs(quote.discount_amount - exact) <= Decimal("0.005")


@given(catalogs_and_lines(), discounts, st.data())
def test_line_order_does_not_change_totals(catalog_and_lines, discount, data):
    catalog, lines = catalog_and_lines
    shuffled = data.draw(st.permutations(lines))

    original = price_quote(catalog, lines, PRICE_DATE, discount)
    reordered = price_quote(catalog, shuffled, PRICE_DATE, discount)
    assert reordered.subtotal == original.subtotal
    assert reordered.total == original.total


@given(catalogs_and_lines(), discounts)
def test_discount_flag_matches_discount(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    assert quote.has_discount == (discount > 0)
    if discount == 0:
        assert quote.total == quote.subtotal


@given(catalogs_and_lines(), discounts)
def test_same_input_gives_same_quote(catalog_and_lines, discount):
    catalog, lines = catalog_and_lines
    assert price_quote(catalog, lines, PRICE_DATE, discount) == price_quote(
        catalog, lines, PRICE_DATE, discount
    )


@given(catalogs_and_lines(), discounts, st.data())
def test_any_unknown_product_stops_the_whole_quote(catalog_and_lines, discount, data):
    catalog, lines = catalog_and_lines
    position = data.draw(st.integers(0, len(lines)))
    with_unknown = list(lines)
    with_unknown.insert(position, QuoteLine("NOT-IN-CATALOG", 1))

    with pytest.raises(NoCurrentPrice):
        price_quote(catalog, with_unknown, PRICE_DATE, discount)

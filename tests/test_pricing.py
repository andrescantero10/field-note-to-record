"""Tests for the pricing engine.

Part 1: the worked examples from docs/PRICING_SPEC.md, plus input checks.
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
D = Decimal


@pytest.fixture(scope="module")
def catalog():
    return Catalog.from_csv(SAMPLE)


def one_product_catalog(price, tax_rate):
    return Catalog([PriceEntry("P-1", "Test product", D(price), D(tax_rate), True, date(2026, 1, 1))])


# =============================================================================
# Part 1: worked examples (docs/PRICING_SPEC.md section 8)
# =============================================================================


def test_example_a_mixed_taxed_and_untaxed_lines_with_discount(catalog):
    quote = price_quote(
        catalog,
        [QuoteLine("ARM-6KG", 2), QuoteLine("WAR-ARM-2Y", 1), QuoteLine("SVC-SHIP", 1)],
        TODAY,
        D("2000.00"),
    )
    assert [(l.amount, l.tax, l.line_total) for l in quote.lines] == [
        (D("36900.00"), D("2398.50"), D("39298.50")),
        (D("3600.00"), D("234.00"), D("3834.00")),
        (D("2450.00"), D("0.00"), D("2450.00")),
    ]
    assert quote.total_before_tax == D("42950.00")
    assert quote.total_tax == D("2632.50")
    assert quote.subtotal == D("45582.50")
    assert quote.discount == D("2000.00")
    assert quote.grand_total == D("43582.50")
    assert quote.has_discount


def test_example_b_tax_is_rounded_to_the_cent(catalog):
    quote = price_quote(catalog, [QuoteLine("GRIP-VAC-4", 1)], TODAY)
    assert quote.lines[0].tax == D("121.91")  # 121.9075
    assert quote.grand_total == D("1997.41")


def test_example_c_half_cent_rounds_up_not_to_even():
    # 5.00 x 6.5% = 0.325 exactly. Half-up gives 0.33; Python's default
    # (half-even) would give 0.32.
    quote = price_quote(one_product_catalog("5.00", "6.50"), [QuoteLine("P-1", 1)], TODAY)
    assert quote.lines[0].tax == D("0.33")


def test_example_d_date_picks_the_catalog_row(catalog):
    before = price_quote(catalog, [QuoteLine("ARM-6KG", 1)], date(2026, 12, 31))
    after = price_quote(catalog, [QuoteLine("ARM-6KG", 1)], date(2027, 1, 1))
    assert before.lines[0].unit_price == D("18450.00")
    assert after.lines[0].unit_price == D("19200.00")
    assert after.lines[0].price_effective_date == date(2027, 1, 1)


@pytest.mark.parametrize(
    "product_id, on_date, reason",
    [
        ("VIS-3D", TODAY, "not yet effective"),
        ("GRIP-2F-50", date(2026, 10, 1), "inactive"),
        ("NOPE-1", TODAY, "unknown product"),
    ],
)
def test_example_e_one_unpriceable_line_stops_the_whole_quote(catalog, product_id, on_date, reason):
    lines = [QuoteLine("ARM-6KG", 1), QuoteLine(product_id, 1), QuoteLine("MNT-PED", 2)]
    with pytest.raises(NoCurrentPrice) as stop:
        price_quote(catalog, lines, on_date)
    assert stop.value.reason == reason


def test_example_e_discount_larger_than_subtotal_is_refused(catalog):
    # 1 x MNT-PED: 620.00 + 40.30 tax = 660.30
    with pytest.raises(ValueError, match="larger than the subtotal"):
        price_quote(catalog, [QuoteLine("MNT-PED", 1)], TODAY, D("700.00"))


def test_discount_equal_to_subtotal_is_allowed(catalog):
    quote = price_quote(catalog, [QuoteLine("MNT-PED", 1)], TODAY, D("660.30"))
    assert quote.grand_total == D("0.00")


def test_no_discount_quote(catalog):
    quote = price_quote(catalog, [QuoteLine("SVC-INSTALL", 3)], TODAY)
    assert quote.total_tax == D("0.00")
    assert quote.grand_total == quote.subtotal == D("4350.00")
    assert not quote.has_discount


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
        price_quote(catalog, [QuoteLine("ARM-6KG", 1)], TODAY, 500.0)


@pytest.mark.parametrize("discount", ["-1", "5.125", "NaN", "Infinity"])
def test_badly_formed_discount_is_refused(catalog, discount):
    with pytest.raises(ValueError, match="discount"):
        price_quote(catalog, [QuoteLine("ARM-6KG", 1)], TODAY, D(discount))


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

# Prices from $0.01 to $100,000.00 and tax rates of 0% or 0.01%-99.99%, built
# from whole hundredths so they have exactly two decimals, like the loader.
prices = st.integers(min_value=1, max_value=10_000_000).map(lambda c: D(c).scaleb(-2))
tax_rates = st.one_of(
    st.just(D("0.00")),
    st.integers(min_value=1, max_value=9_999).map(lambda c: D(c).scaleb(-2)),
)


@st.composite
def quotes(draw):
    """A random catalog of 1-8 products, a random quote of 1-10 lines from it,
    and a random valid discount (0 up to the subtotal)."""
    product_ids = [f"P{i}" for i in range(draw(st.integers(1, 8)))]
    entries = [
        PriceEntry(pid, f"Product {pid}", draw(prices), draw(tax_rates), True, PRICE_DATE)
        for pid in product_ids
    ]
    catalog = Catalog(entries)
    lines = draw(
        st.lists(
            st.builds(QuoteLine, st.sampled_from(product_ids), st.integers(1, 1_000)),
            min_size=1,
            max_size=10,
        )
    )
    # The upper bound for the discount is the subtotal, so price once without one.
    subtotal_cents = int(price_quote(catalog, lines, PRICE_DATE).subtotal * 100)
    discount = D(draw(st.integers(0, subtotal_cents))).scaleb(-2)
    return catalog, lines, discount


def is_cents(amount):
    return amount.as_tuple().exponent == -2


@given(quotes())
def test_each_line_adds_up(q):
    catalog, lines, discount = q
    for line in price_quote(catalog, lines, PRICE_DATE, discount).lines:
        assert line.amount == line.unit_price * line.quantity
        assert line.line_total == line.amount + line.tax


@given(quotes())
def test_line_tax_is_within_half_a_cent_of_exact(q):
    catalog, lines, discount = q
    for line in price_quote(catalog, lines, PRICE_DATE, discount).lines:
        exact = line.amount * line.tax_rate / 100
        assert abs(line.tax - exact) <= D("0.005")
        if line.tax_rate == 0:
            assert line.tax == 0


@given(quotes())
def test_quote_totals_add_up(q):
    catalog, lines, discount = q
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    assert quote.total_before_tax == sum(l.amount for l in quote.lines)
    assert quote.total_tax == sum(l.tax for l in quote.lines)
    assert quote.subtotal == sum(l.line_total for l in quote.lines)
    assert quote.subtotal == quote.total_before_tax + quote.total_tax
    assert quote.grand_total == quote.subtotal - quote.discount


@given(quotes())
def test_prices_and_tax_rates_come_only_from_the_catalog(q):
    catalog, lines, discount = q
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    assert len(quote.lines) == len(lines)
    for requested, priced in zip(lines, quote.lines):
        entry = catalog.current_price(requested.product_id, PRICE_DATE)
        assert priced.product_id == requested.product_id
        assert priced.quantity == requested.quantity
        assert priced.unit_price == entry.unit_price
        assert priced.tax_rate == entry.tax_rate


@given(quotes())
def test_amounts_are_whole_cents_and_never_negative(q):
    catalog, lines, discount = q
    quote = price_quote(catalog, lines, PRICE_DATE, discount)

    amounts = [quote.total_before_tax, quote.total_tax, quote.subtotal, quote.discount, quote.grand_total]
    for line in quote.lines:
        amounts += [line.amount, line.tax, line.line_total]
    for amount in amounts:
        assert is_cents(amount)
        assert amount >= 0
    assert quote.grand_total <= quote.subtotal


@given(quotes(), st.data())
def test_line_order_does_not_change_totals(q, data):
    catalog, lines, discount = q
    shuffled = data.draw(st.permutations(lines))

    original = price_quote(catalog, lines, PRICE_DATE, discount)
    reordered = price_quote(catalog, shuffled, PRICE_DATE, discount)
    assert reordered.total_tax == original.total_tax
    assert reordered.subtotal == original.subtotal
    assert reordered.grand_total == original.grand_total


@given(quotes())
def test_discount_flag_matches_discount(q):
    catalog, lines, discount = q
    quote = price_quote(catalog, lines, PRICE_DATE, discount)
    assert quote.has_discount == (discount > 0)


@given(quotes())
def test_same_input_gives_same_quote(q):
    catalog, lines, discount = q
    assert price_quote(catalog, lines, PRICE_DATE, discount) == price_quote(
        catalog, lines, PRICE_DATE, discount
    )


@given(quotes(), st.integers(1, 10_000_000))
def test_discount_above_subtotal_is_always_refused(q, extra_cents):
    catalog, lines, _ = q
    subtotal = price_quote(catalog, lines, PRICE_DATE).subtotal
    with pytest.raises(ValueError, match="larger than the subtotal"):
        price_quote(catalog, lines, PRICE_DATE, subtotal + D(extra_cents).scaleb(-2))


@given(quotes(), st.data())
def test_any_unknown_product_stops_the_whole_quote(q, data):
    catalog, lines, discount = q
    position = data.draw(st.integers(0, len(lines)))
    with_unknown = list(lines)
    with_unknown.insert(position, QuoteLine("NOT-IN-CATALOG", 1))

    with pytest.raises(NoCurrentPrice):
        price_quote(catalog, with_unknown, PRICE_DATE, discount)

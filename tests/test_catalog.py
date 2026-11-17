"""Tests for the product catalog."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fieldnote.catalog import Catalog, CatalogError, NoCurrentPrice

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "catalog.csv"
HEADER = "id,name,unit_price,tax_rate,active,effective_date\n"
GOOD_ROW = "X-1,Test item,100.00,6.50,true,2026-01-01\n"


@pytest.fixture(scope="module")
def catalog():
    return Catalog.from_csv(SAMPLE)


def write_csv(tmp_path, text):
    path = tmp_path / "catalog.csv"
    path.write_text(text, encoding="utf-8")
    return path


# --- The sample catalog -------------------------------------------------------


def test_sample_catalog_loads_with_decimal_prices_and_tax_rates(catalog):
    entry = catalog.current_price("GRIP-VAC-4", date(2026, 11, 14))
    assert entry.unit_price == Decimal("1875.50")
    assert entry.tax_rate == Decimal("6.50")
    assert isinstance(entry.unit_price, Decimal)
    assert isinstance(entry.tax_rate, Decimal)
    assert entry.name == "Vacuum gripper with 4 suction cups"


def test_untaxed_item_has_zero_tax_rate(catalog):
    assert catalog.current_price("SVC-SHIP", date(2026, 11, 14)).tax_rate == Decimal("0.00")


@pytest.mark.parametrize(
    "on_date, expected",
    [
        (date(2026, 6, 30), Decimal("25900.00")),  # day before the change
        (date(2026, 7, 1), Decimal("26900.00")),   # the change applies on its date
        (date(2026, 11, 14), Decimal("26900.00")),
    ],
)
def test_latest_price_on_or_before_date_applies(catalog, on_date, expected):
    assert catalog.current_price("ARM-12KG", on_date).unit_price == expected


def test_future_price_is_not_used_early(catalog):
    assert catalog.current_price("ARM-6KG", date(2026, 12, 31)).unit_price == Decimal("18450.00")
    assert catalog.current_price("ARM-6KG", date(2027, 1, 1)).unit_price == Decimal("19200.00")


# --- Hard stops ---------------------------------------------------------------


def test_unknown_product_is_a_hard_stop(catalog):
    with pytest.raises(NoCurrentPrice) as stop:
        catalog.current_price("ARM-99KG", date(2026, 11, 14))
    assert stop.value.reason == "unknown product"


def test_product_id_must_match_exactly(catalog):
    with pytest.raises(NoCurrentPrice):
        catalog.current_price("arm-6kg", date(2026, 11, 14))


def test_product_not_yet_available_is_a_hard_stop(catalog):
    with pytest.raises(NoCurrentPrice) as stop:
        catalog.current_price("VIS-3D", date(2026, 11, 14))
    assert stop.value.reason == "not yet effective"


def test_discontinued_product_is_a_hard_stop_with_no_fallback(catalog):
    # Active at 2480.00 until 2026-09-01, then marked inactive. The old price
    # must not be used after that date.
    assert catalog.current_price("GRIP-2F-50", date(2026, 8, 31)).unit_price == Decimal("2480.00")
    with pytest.raises(NoCurrentPrice) as stop:
        catalog.current_price("GRIP-2F-50", date(2026, 10, 1))
    assert stop.value.reason == "inactive"


def test_datetime_instead_of_date_is_refused(catalog):
    with pytest.raises(TypeError):
        catalog.current_price("ARM-6KG", datetime(2026, 11, 14, 9, 0))


# --- Accepted formats ---------------------------------------------------------


def test_whole_numbers_and_excel_booleans_are_accepted(tmp_path):
    path = write_csv(tmp_path, HEADER + "X-1,Test item,1200,7,TRUE,2026-01-01\n")
    entry = Catalog.from_csv(path).current_price("X-1", date(2026, 2, 1))
    assert entry.unit_price == Decimal("1200.00")
    assert entry.tax_rate == Decimal("7.00")
    assert entry.active is True


# --- Malformed files are rejected at load time --------------------------------


@pytest.mark.parametrize(
    "bad_row",
    [
        # unit_price
        "X-2,Test item,-5.00,6.50,true,2026-01-01",       # negative
        "X-2,Test item,0.00,6.50,true,2026-01-01",        # zero
        "X-2,Test item,12.345,6.50,true,2026-01-01",      # more than 2 decimals
        'X-2,Test item,"1,200.00",6.50,true,2026-01-01',  # thousands separator
        "X-2,Test item,$100,6.50,true,2026-01-01",        # currency symbol
        "X-2,Test item,1e3,6.50,true,2026-01-01",         # scientific notation
        "X-2,Test item,NaN,6.50,true,2026-01-01",
        "X-2,Test item,,6.50,true,2026-01-01",            # missing price
        # tax_rate
        "X-2,Test item,100.00,7%,true,2026-01-01",        # percent sign
        "X-2,Test item,100.00,-1,true,2026-01-01",        # negative
        "X-2,Test item,100.00,100,true,2026-01-01",       # 100% or more
        "X-2,Test item,100.00,6.555,true,2026-01-01",     # more than 2 decimals
        "X-2,Test item,100.00,,true,2026-01-01",          # missing
        "X-2,Test item,100.00,0.065,true,2026-01-01",     # a fraction, not a percent
        # active
        "X-2,Test item,100.00,6.50,yes,2026-01-01",
        "X-2,Test item,100.00,6.50,,2026-01-01",
        # effective_date
        "X-2,Test item,100.00,6.50,true,11/14/2026",      # US-style date
        "X-2,Test item,100.00,6.50,true,20261114",        # compact date
        "X-2,Test item,100.00,6.50,true,2026-13-01",      # month 13
        # id, name, shape
        ",Test item,100.00,6.50,true,2026-01-01",         # blank id
        "X-2,,100.00,6.50,true,2026-01-01",               # blank name
        "X-2,Gripper, 2 finger,100.00,6.50,true,2026-01-01",  # unquoted comma
    ],
)
def test_bad_row_is_rejected_with_its_line_number(tmp_path, bad_row):
    path = write_csv(tmp_path, HEADER + GOOD_ROW + bad_row + "\n")
    with pytest.raises(CatalogError, match="line 3"):
        Catalog.from_csv(path)


def test_duplicate_effective_date_for_a_product_is_rejected(tmp_path):
    path = write_csv(tmp_path, HEADER + GOOD_ROW + "X-1,Test item,120.00,6.50,true,2026-01-01\n")
    with pytest.raises(CatalogError, match="same effective_date"):
        Catalog.from_csv(path)


@pytest.mark.parametrize(
    "header",
    [
        "id,name,unit_price,active,effective_date\n",                    # old file, no tax_rate
        "id,name,unit_price,tax_rate,active,effective_date,notes\n",     # extra column
        "sku,name,unit_price,tax_rate,active,effective_date\n",          # renamed column
    ],
)
def test_wrong_columns_are_rejected(tmp_path, header):
    path = write_csv(tmp_path, header + GOOD_ROW)
    with pytest.raises(CatalogError, match="columns"):
        Catalog.from_csv(path)


def test_empty_catalog_is_rejected(tmp_path):
    with pytest.raises(CatalogError, match="empty"):
        Catalog.from_csv(write_csv(tmp_path, HEADER))


# --- What the model is allowed to see ------------------------------------------


def test_product_names_lists_ids_and_names_only(catalog):
    names = catalog.product_names()
    assert names["ARM-6KG"] == "Collaborative arm 6-axis 6 kg payload"
    assert names["SVC-SHIP"] == "Shipping and deployment per robot"
    # Discontinued and future products are listed too; pricing stops them later.
    assert {"GRIP-2F-50", "VIS-3D"} <= set(names)
    assert len(names) == 15
    assert all(isinstance(name, str) for name in names.values())
    assert "18450" not in repr(names) and "6.50" not in repr(names)

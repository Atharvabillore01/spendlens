"""Parsing a block of cells copied out of a spreadsheet.

Every case here is a shape real client data actually arrives in. The reason
this file is long is that the failure mode is silent: a mis-parsed amount or a
date read in the wrong order does not raise, it just produces a wrong answer
that looks right.
"""

from __future__ import annotations

import pytest

from src.ingest.loader import IngestError
from src.ingest.paste import (
    is_ambiguous_date,
    looks_like_header,
    parse_paste,
    sniff_delimiter,
)

HEADER = "user_id\tuser_name\tdate\tamount\tcategory\tmerchant"


def row(*cells: str) -> str:
    return "\t".join(cells)


# == delimiters ===============================================================


def test_excel_pastes_are_tabs_and_win_over_stray_commas():
    text = "a\tb\tPizza, Large\nc\td\tSoup, Hot"
    assert sniff_delimiter(text) == "\t"


@pytest.mark.parametrize("delimiter", [",", ";", "|"])
def test_other_delimiters_are_sniffed(delimiter):
    text = delimiter.join(["a", "b", "c"]) + "\n" + delimiter.join(["d", "e", "f"])
    assert sniff_delimiter(text) == delimiter


# == headers ==================================================================


def test_a_row_of_numbers_is_not_a_header():
    assert not looks_like_header(["usr_a", "Jose", "2025-01-01", "10", "X_FOOD"])


def test_unfamiliar_header_names_are_still_recognised_as_a_header():
    """Requiring known aliases would reject every export we haven't seen."""
    assert looks_like_header(["Reference", "Client", "Booked", "Debit", "Bucket"])


def test_headers_are_matched_loosely():
    text = "user_id\tuser_name\tTxn Date\tAmount (USD)\tCategory\tPayee\n" + row(
        "usr_a", "Jose", "2025-11-02", "10.00", "COFFEE_FOOD", "Blue Bottle"
    )
    frame, report = parse_paste(text)
    assert report.column_mapping["Txn Date"] == "transaction_date"
    assert report.column_mapping["Amount (USD)"] == "transaction_amount"
    assert report.column_mapping["Payee"] == "merchant_name"
    assert report.ok


def test_a_bare_range_with_no_header_uses_the_canonical_order():
    frame, report = parse_paste(row("usr_a", "Jose", "2025-11-02", "10", "COFFEE_FOOD", "Blue Bottle"))
    assert report.header_used is False
    assert report.rows_parsed == 1
    assert frame["merchant_name"].iloc[0] == "Blue Bottle"


# == amounts ==================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1850", 1850.0),
        ("$1,850.00", 1850.0),
        ("1 234,56", 1234.56),      # European, non-breaking space
        ("1.234,56", 1234.56),      # European thousands + decimal
        ("(89.00)", -89.0),         # accounting negative
        ("-89.00", -89.0),
        ("$ 12", 12.0),
    ],
)
def test_amount_formats(raw, expected):
    frame, report = parse_paste(HEADER + "\n" + row("usr_a", "Jose", "2025-11-02", raw, "X_FOOD", "M"))
    assert report.rows_parsed == 1
    assert float(frame["transaction_amount"].iloc[0]) == pytest.approx(expected)


def test_accounting_negatives_are_not_silently_positive():
    """Losing a parenthesised minus turns income into spending."""
    frame, _ = parse_paste(HEADER + "\n" + row("usr_a", "Jose", "2025-11-02", "(1,500.00)", "SALARY_INCOME", "Acme"))
    assert float(frame["transaction_amount"].iloc[0]) == -1500.0


# == dates ====================================================================


@pytest.mark.parametrize(
    "raw,iso",
    [
        ("2025-12-31", "2025-12-31"),
        ("31/12/2025", "2025-12-31"),   # unambiguous: 31 cannot be a month
        ("45658", "2025-01-01"),        # Excel serial, 1899-12-30 origin
    ],
)
def test_date_formats(raw, iso):
    frame, _ = parse_paste(HEADER + "\n" + row("usr_a", "Jose", raw, "10", "X_FOOD", "M"))
    assert str(frame["transaction_date"].iloc[0].date()) == iso


@pytest.mark.parametrize("raw", ["05/01/2026", "1-2-2025", "3.4.2025"])
def test_ambiguous_dates_are_detected(raw):
    assert is_ambiguous_date(raw)


@pytest.mark.parametrize("raw", ["31/12/2025", "2025-12-31", "13/01/2025", ""])
def test_unambiguous_dates_are_not_flagged(raw):
    assert not is_ambiguous_date(raw)


def test_an_ambiguous_date_warns_rather_than_guessing_quietly():
    """05/01 is January 5th in most of the world and May 1st in the US. Picking
    one silently is how a financial report ends up four months wrong."""
    _, report = parse_paste(HEADER + "\n" + row("usr_a", "Jose", "05/01/2026", "10", "X_FOOD", "M"))
    assert any("ambiguous" in note for note in report.coercion_notes)


# == robustness ===============================================================


def test_quoted_delimiters_survive():
    text = 'user_id,user_name,date,amount,category,merchant\nusr_a,Jose,2025-11-02,"1,299.99",X_SHOPPING,"Apple, Inc."'
    frame, _ = parse_paste(text)
    assert float(frame["transaction_amount"].iloc[0]) == 1299.99
    assert frame["merchant_name"].iloc[0] == "Apple, Inc."


def test_blank_and_ragged_rows_are_handled():
    text = HEADER + "\n" + row("usr_a", "Jose", "2025-11-02", "10", "X_FOOD", "M") + "\n\t\t\t\t\t\n" + "usr_a\tJose\t2025-11-03\t20\tX_FOOD"
    frame, report = parse_paste(text)
    assert report.rows_parsed == 2, "the short row is padded, the blank row dropped"


def test_unreadable_rows_are_dropped_and_reported():
    text = (
        HEADER
        + "\n" + row("usr_a", "Jose", "not-a-date", "10", "X_FOOD", "M")
        + "\n" + row("usr_a", "Jose", "2025-11-02", "nonsense", "X_FOOD", "M")
        + "\n" + row("usr_a", "Jose", "2025-11-03", "5", "X_FOOD", "M")
    )
    _, report = parse_paste(text)
    assert report.rows_parsed == 1
    assert any("amount" in n for n in report.coercion_notes)
    assert any("date" in n for n in report.coercion_notes)


def test_missing_required_columns_block_the_import():
    _, report = parse_paste("date\tamount\n2025-01-01\t10")
    assert not report.ok
    assert "user_id" in report.missing_required


def test_column_overrides_correct_a_mis_detected_column():
    text = "who\twhat\twhen\thow_much\tkind\n" + row("usr_a", "Jose", "2025-11-02", "10", "X_FOOD")
    _, before = parse_paste(text)
    assert before.missing_required

    _, after = parse_paste(
        text,
        column_overrides={
            "0": "user_id",
            "1": "user_name",
            "2": "transaction_date",
            "3": "transaction_amount",
            "4": "transaction_category_detail",
        },
    )
    assert after.ok and after.rows_parsed == 1


def test_empty_paste_is_a_clean_error():
    with pytest.raises(IngestError, match="nothing to import"):
        parse_paste("   \n  \n")


def test_a_header_with_no_body_is_a_clean_error():
    with pytest.raises(IngestError, match="no data rows"):
        parse_paste(HEADER)

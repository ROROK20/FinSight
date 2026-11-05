"""The no-key regex fallback reads a field's level with its sign, or leaves it empty.

The report prints these as reported figures, so a loss must not read as a profit, or a
change as a level.
"""

import pytest

from finsight.extraction.kpi_extractor import KPIExtractor
from finsight.report.generator import ReportGenerator


def read(text):
    extractor = KPIExtractor()
    extractor._client = None
    return extractor.extract_from_text(text, "X", "FY2025")


@pytest.mark.parametrize("text, field, expected", [
    ("Free cash flow was negative $1.2 billion.", "free_cash_flow", -1_200.0),
    ("Free cash flow was a negative $1.2 billion.", "free_cash_flow", -1_200.0),
    ("Free cash flow turned negative at $1.2 billion.", "free_cash_flow", -1_200.0),
    ("Free cash flow was an outflow of $1.2 billion.", "free_cash_flow", -1_200.0),
    ("Free cash flow was a $1.2 billion outflow.", "free_cash_flow", -1_200.0),
    ("Free cash flow was ($1.2 billion).", "free_cash_flow", -1_200.0),
    ("Free cash flow ($1.2 billion).", "free_cash_flow", -1_200.0),
    ("Free cash flow: ($1.2 billion)", "free_cash_flow", -1_200.0),
    ("Free cash flow was $(1.2) billion.", "free_cash_flow", -1_200.0),
    ("Net income was -$3.0 billion.", "net_income", -3_000.0),
    ("Net income was −$3.0 billion.", "net_income", -3_000.0),   # U+2212
    ("Net income was $-3.0 billion.", "net_income", -3_000.0),
    ("Net income: -$3.0B", "net_income", -3_000.0),
    ("Net income $ (3,000)", "net_income", -3_000.0),            # a table row, $M
    ("Net income was a loss of $3.0 billion.", "net_income", -3_000.0),
    ("Net income was a $3.0 billion loss.", "net_income", -3_000.0),
    ("Net income (loss) was ($3.0 billion).", "net_income", -3_000.0),
    ("Net loss of $1.2 billion.", "net_income", -1_200.0),
    ("Net loss was $1.2 billion.", "net_income", -1_200.0),
    ("Net losses of $1.2 billion.", "net_income", -1_200.0),
    ("EBITDA loss of $200 million.", "ebitda", -200.0),
    ("EBITDA was negative $200M.", "ebitda", -200.0),
    ("Operating margin was -3.2%.", "operating_margin", -0.032),
    ("Operating margin was negative 3.2%.", "operating_margin", -0.032),
    ("Operating margin was (3.2%).", "operating_margin", -0.032),
    ("Gross margin was −1.5%.", "gross_margin", -0.015),
])
def test_a_negative_form_reads_negative(text, field, expected):
    assert getattr(read(text), field) == pytest.approx(expected)


@pytest.mark.parametrize("text, field, expected", [
    ("Revenue increased $5.1 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue decreased $5.1 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue rose $5.1 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue fell $5.1 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue grew by $5.1 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue increased $5.1 billion, or 6%, to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue increased $5.1 billion due to demand, to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue increased $5.1 billion, reaching $94.9 billion.", "revenue", 94_900.0),
    ("Revenue rose from $89.8 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue rose $5.1 billion from $89.8 billion to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue increased to $94.9 billion from $89.8 billion.", "revenue", 94_900.0),
    ("Revenue grew 8% to $94.9 billion.", "revenue", 94_900.0),
    ("Revenue rose to a record $94.9 billion.", "revenue", 94_900.0),
    ("Revenue was higher at $94.9 billion.", "revenue", 94_900.0),
    ("Revenue, up 5%, was $94.9 billion.", "revenue", 94_900.0),
    ("Net income fell $1 billion to $2 billion.", "net_income", 2_000.0),
    ("Net income fell $1 billion to a loss of $2 billion.", "net_income", -2_000.0),
    ("Net income fell $1 billion to -$2 billion.", "net_income", -2_000.0),
    ("Net income fell $1 billion to ($2 billion).", "net_income", -2_000.0),
    ("Net income decreased $4 billion to negative $2 billion.", "net_income", -2_000.0),
    ("Net loss widened $1 billion to $3 billion.", "net_income", -3_000.0),
    ("Net loss narrowed to $3 billion.", "net_income", -3_000.0),
    ("Free cash flow improved $1.2 billion to $3 billion.", "free_cash_flow", 3_000.0),
    ("Gross margin expanded 2.0% to 45%.", "gross_margin", 0.45),
    ("Gross margin expanded 120 basis points to 45%.", "gross_margin", 0.45),
    ("Operating margin fell from 12% to 9%.", "operating_margin", 0.09),
])
def test_a_change_with_a_level_reads_the_level(text, field, expected):
    assert getattr(read(text), field) == pytest.approx(expected)


@pytest.mark.parametrize("text, field", [
    ("Revenue increased $5.1 billion.", "revenue"),
    ("Revenue increased by $5.1 billion year over year.", "revenue"),
    ("Revenue was up $5.1 billion.", "revenue"),
    ("Revenue fell $5.1 billion.", "revenue"),
    ("Revenue saw an increase of $5.1 billion.", "revenue"),
    ("Revenue saw a $5.1 billion increase.", "revenue"),
    ("Revenue was $5.1 billion higher than last year.", "revenue"),
    ("Revenue was $5.1 billion more than last year.", "revenue"),
    ("Revenue increased $5.1 billion compared to $89.8 billion.", "revenue"),
    ("Revenue compared with $89.8 billion last year.", "revenue"),
    ("Net income fell $1 billion due to a charge of $2 billion.", "net_income"),
    ("Net income declined $1 billion.", "net_income"),
    ("Net income reflects a loss of $1 billion on investments.", "net_income"),
    ("Net income included a $1 billion loss on investments.", "net_income"),
    ("Net income for the year ($3 billion) rose 5%.", "net_income"),
    ("We reported strong free cash flow ($1.2 billion).", "free_cash_flow"),
    ("Free cash flow was $1.2 billion above last year.", "free_cash_flow"),
    ("Revenue loss of $1 billion from the outage.", "revenue"),
    ("Gross margin expanded 2.0%.", "gross_margin"),
    ("Gross margin was 2.0% higher.", "gross_margin"),
    ("Operating margin fell 3%.", "operating_margin"),
])
def test_a_sentence_with_only_a_change_leaves_the_field_empty(text, field):
    assert getattr(read(text), field) is None


@pytest.mark.parametrize("text, field", [
    ("Revenue was strong. Profit was $3 billion.", "revenue"),          # another sentence
    ("Revenue grew while net income was $3 billion.", "revenue"),       # another field
    ("Net income per share was $1.20.", "net_income"),                  # not the total
    ("Cost of revenue was $50 billion.", "revenue"),
    ("Deferred revenue was $5 billion.", "revenue"),
    ("Gross margin was strong. Demand grew 10%.", "gross_margin"),
])
def test_a_figure_that_is_not_the_fields_leaves_it_empty(text, field):
    assert getattr(read(text), field) is None


@pytest.mark.parametrize("text, field, expected", [
    ("Revenue was $94.9 billion.", "revenue", 94_900.0),
    ("Revenue: $94.9B", "revenue", 94_900.0),
    ("Revenue - $94.9 billion", "revenue", 94_900.0),                   # a separator
    ("Revenue — $94.9 billion", "revenue", 94_900.0),
    ("Revenue-$94.9 billion", "revenue", 94_900.0),
    ("Revenue\n$94.9 billion", "revenue", 94_900.0),                    # a table cell
    ("Revenue ($94.9 billion) rose 5%.", "revenue", 94_900.0),          # an aside
    ("Revenue of $94.9 billion, up $5.1 billion.", "revenue", 94_900.0),
    ("Revenue of $94.9 billion, up 8% year over year.", "revenue", 94_900.0),
    ("Revenue of about $94.9 billion.", "revenue", 94_900.0),
    ("Revenue of more than $94.9 billion.", "revenue", 94_900.0),
    ("Revenue from contracts with customers was $94.9 billion.", "revenue", 94_900.0),
    ("Revenues were $94.9 billion.", "revenue", 94_900.0),
    ("Net sales were $5.2bn.", "revenue", 5_200.0),
    ("Cost of revenue was $50 billion. Revenue was $94.9 billion.", "revenue", 94_900.0),
    ("Revenue increased $5.1 billion. Full-year revenue was $94.9 billion.", "revenue", 94_900.0),
    ("Net income was $3.0 billion.", "net_income", 3_000.0),
    ("Net income - $3.0 billion", "net_income", 3_000.0),               # a separator
    ("Net income (loss) was $3.0 billion.", "net_income", 3_000.0),     # a caption
    ("Net income (loss) of $3.0 billion.", "net_income", 3_000.0),
    ("Net income was $3.0 billion despite a loss of $1 billion.", "net_income", 3_000.0),
    ("Net income was $3.0 billion, with diluted EPS of $6.13.", "net_income", 3_000.0),
    ("EBITDA (operating income plus D&A) was $1.5 billion.", "ebitda", 1_500.0),
    ("Adjusted EBITDA was $1.5B.", "ebitda", 1_500.0),
    ("Free cash flow was $1.2 billion.", "free_cash_flow", 1_200.0),
    ("Free cash flow (operating cash flow less CapEx) was $1.2 billion.", "free_cash_flow", 1_200.0),
    ("Gross margin was 44.1%, while operating margin reached 29.8%.", "gross_margin", 0.441),
    ("Gross margin was 44.1%, while operating margin reached 29.8%.", "operating_margin", 0.298),
    ("Our gross margin expanded to 68.3%.", "gross_margin", 0.683),
    ("Gross margin was 45%, up 2%.", "gross_margin", 0.45),
])
def test_a_plain_level_still_reads(text, field, expected):
    assert getattr(read(text), field) == pytest.approx(expected)


def test_a_sentence_reads_each_field_on_its_own():
    k = read("Total revenue was $94.9B and net income was -$15.3B. Free cash flow was "
             "negative $1.2 billion, and gross margin was 44.1%.")
    assert k.revenue == pytest.approx(94_900.0)
    assert k.net_income == pytest.approx(-15_300.0)
    assert k.free_cash_flow == pytest.approx(-1_200.0)
    assert k.gross_margin == pytest.approx(0.441)


def test_the_report_prints_a_negative_free_cash_flow_as_negative():
    kpis = read("Revenue was $94.9 billion. Free cash flow was negative $1.2 billion.")
    report = ReportGenerator().generate(ticker="X", kpis=kpis)
    assert "Reported free cash flow of -$1,200M" in report.key_highlights
    assert "Reported free cash flow of -$1,200M" in ReportGenerator().to_markdown(report)

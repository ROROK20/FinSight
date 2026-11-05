"""
Tests for the financial-statement normalization layer.

``financial_parser`` is documented in docs/architecture.md as a real layer of
the pipeline. It is wired into ``KPIExtractor.extract_from_xbrl`` (revenue growth),
and this module pins both the primitives and that integration.

Runs fully offline — the fixtures are literal frames and XBRL-shaped dicts.
"""

import numpy as np
import pandas as pd
import pytest

from finsight.extraction.financial_parser import FinancialHistory, FinancialParser
from finsight.extraction.kpi_extractor import KPIExtractor


@pytest.fixture()
def parser() -> FinancialParser:
    return FinancialParser()


def _xbrl(concept_values: dict[str, list[tuple[str, float]]]) -> dict[str, list[dict]]:
    """Build an XBRL-shaped series: {concept: [{end, val, form}, ...]}."""
    return {
        concept: [{"end": end, "val": val, "form": "10-K"} for end, val in entries]
        for concept, entries in concept_values.items()
    }


# ---------------------------------------------------------------------------
# compute_growth_rates
# ---------------------------------------------------------------------------

def test_growth_rates_yoy_and_cagr(parser):
    series = pd.Series(
        [100.0, 110.0, 121.0, 133.1],
        index=pd.to_datetime(["2020-12-31", "2021-12-31", "2022-12-31", "2023-12-31"]),
    )
    rates = parser.compute_growth_rates(series)
    assert rates["yoy_1y"] == pytest.approx(0.10)
    assert rates["yoy_2y"] == pytest.approx(0.21)
    assert rates["cagr_3y"] == pytest.approx(0.10)
    # Only four points, so a five-year CAGR is not computable.
    assert rates["cagr_5y"] is None


def test_growth_rates_need_two_points(parser):
    single = pd.Series([100.0], index=pd.to_datetime(["2023-12-31"]))
    assert parser.compute_growth_rates(single) == {}


def test_growth_rates_sort_by_index_not_input_order(parser):
    shuffled = pd.Series(
        [121.0, 100.0, 110.0],
        index=pd.to_datetime(["2022-12-31", "2020-12-31", "2021-12-31"]),
    )
    assert parser.compute_growth_rates(shuffled)["yoy_1y"] == pytest.approx(0.10)


def test_growth_rate_is_none_when_the_base_year_is_zero(parser):
    """Guards a divide-by-zero rather than returning inf."""
    series = pd.Series([0.0, 50.0], index=pd.to_datetime(["2022-12-31", "2023-12-31"]))
    assert parser.compute_growth_rates(series)["yoy_1y"] is None


# ---------------------------------------------------------------------------
# compute_margins / ttm
# ---------------------------------------------------------------------------

def test_compute_margins(parser):
    income = pd.DataFrame(
        {
            "Total Revenue": [1000.0, 1200.0],
            "Gross Profit": [400.0, 600.0],
            "Operating Income": [200.0, 300.0],
            "Net Income": [100.0, 180.0],
        },
        index=pd.to_datetime(["2022-12-31", "2023-12-31"]),
    )
    margins = parser.compute_margins(income)
    assert list(margins["gross_margin"]) == pytest.approx([0.40, 0.50])
    assert list(margins["operating_margin"]) == pytest.approx([0.20, 0.25])
    assert list(margins["net_margin"]) == pytest.approx([0.10, 0.15])


def test_compute_margins_missing_columns_returns_empty(parser):
    margins = parser.compute_margins(pd.DataFrame({"Something Else": [1.0]}))
    assert margins["gross_margin"].empty


def test_ttm_sums_the_last_four_quarters(parser):
    quarterly = pd.DataFrame({"Total Revenue": [10.0, 20.0, 30.0, 40.0, 50.0]})
    assert parser.ttm(quarterly, "Total Revenue") == pytest.approx(140.0)


def test_ttm_with_fewer_than_four_quarters_sums_what_exists(parser):
    assert parser.ttm(pd.DataFrame({"Revenue": [10.0, 20.0]}), "Revenue") == pytest.approx(30.0)


def test_ttm_unknown_metric_or_empty_frame(parser):
    assert parser.ttm(pd.DataFrame(), "Revenue") is None
    assert parser.ttm(pd.DataFrame({"Revenue": [1.0]}), "Nonexistent") is None


# ---------------------------------------------------------------------------
# normalize_xbrl_to_df
# ---------------------------------------------------------------------------

def test_normalize_xbrl_converts_to_millions_and_sorts(parser):
    series = _xbrl({"Revenues": [("2023-12-31", 2_000_000_000.0),
                                 ("2022-12-31", 1_000_000_000.0)]})
    df = parser.normalize_xbrl_to_df(series, ["Revenues"])
    assert list(df.index.year) == [2022, 2023]      # sorted ascending
    assert list(df["Revenues"]) == pytest.approx([1_000.0, 2_000.0])  # $M


def test_normalize_xbrl_filters_by_form_type(parser):
    series = {"Revenues": [
        {"end": "2023-12-31", "val": 1e9, "form": "10-K"},
        {"end": "2023-09-30", "val": 3e8, "form": "10-Q"},
    ]}
    annual = parser.normalize_xbrl_to_df(series, ["Revenues"], period_type="annual")
    quarterly = parser.normalize_xbrl_to_df(series, ["Revenues"], period_type="quarterly")
    assert len(annual) == 1 and annual.index[0].year == 2023
    assert len(quarterly) == 1 and quarterly.index[0].month == 9


def test_normalize_xbrl_empty_input(parser):
    assert parser.normalize_xbrl_to_df({}, ["Revenues"]).empty


# ---------------------------------------------------------------------------
# FinancialHistory
# ---------------------------------------------------------------------------

def test_history_series_accessors_and_ebitda_reconstruction(parser):
    income = pd.DataFrame({"Total Revenue": [1000.0], "Operating Income": [200.0]})
    cash_flow = pd.DataFrame({"Depreciation And Amortization": [50.0],
                              "Free Cash Flow": [150.0]})
    history = parser.parse_yfinance(
        "TGT", {"income": income, "cash_flow": cash_flow}
    )
    assert isinstance(history, FinancialHistory)
    assert list(history.revenue_history) == pytest.approx([1000.0])
    # EBITDA = EBIT + D&A when both sides are available.
    assert list(history.ebitda_history) == pytest.approx([250.0])
    assert list(history.fcf_history) == pytest.approx([150.0])


def test_history_missing_statements_yield_empty_series(parser):
    history = parser.parse_yfinance("TGT", {})
    assert history.revenue_history.empty
    assert history.fcf_history.empty
    assert "TGT" in repr(history)


# ---------------------------------------------------------------------------
# Integration: the XBRL KPI path uses the parser
# ---------------------------------------------------------------------------

def test_xbrl_extraction_populates_revenue_growth():
    """The live /kpis path returns a real revenue_growth_yoy."""
    series = _xbrl({"Revenues": [("2022-12-31", 1_000_000_000.0),
                                 ("2023-12-31", 1_250_000_000.0)]})
    kpis = KPIExtractor().extract_from_xbrl(series, "TGT", "2023")
    assert kpis.revenue_growth_yoy == pytest.approx(0.25)


def test_revenue_growth_is_measured_at_the_extracted_period():
    """Growth must describe the period the KPIs describe, not the newest filing.

    Extracting FY2022 while the payload also holds FY2023 must report 2021→2022
    growth, otherwise the reported growth belongs to a different year than every
    other number in the same response.
    """
    series = _xbrl({"Revenues": [("2021-12-31", 1_000_000_000.0),
                                 ("2022-12-31", 1_100_000_000.0),
                                 ("2023-12-31", 2_000_000_000.0)]})
    kpis = KPIExtractor().extract_from_xbrl(series, "TGT", "2022")
    assert kpis.revenue_growth_yoy == pytest.approx(0.10)


def test_revenue_growth_is_none_with_a_single_year():
    series = _xbrl({"Revenues": [("2023-12-31", 1_000_000_000.0)]})
    kpis = KPIExtractor().extract_from_xbrl(series, "TGT", "2023")
    assert kpis.revenue_growth_yoy is None


def test_revenue_growth_handles_the_alternate_revenue_concept():
    concept = "RevenueFromContractWithCustomerExcludingAssessedTax"
    series = _xbrl({concept: [("2022-12-31", 500_000_000.0),
                              ("2023-12-31", 600_000_000.0)]})
    kpis = KPIExtractor().extract_from_xbrl(series, "TGT", "2023")
    assert kpis.revenue_growth_yoy == pytest.approx(0.20)

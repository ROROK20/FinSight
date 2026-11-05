"""Tests for report generation."""

import copy
import datetime

import pytest
from finsight.report.generator import ReportGenerator, EquityReport
from finsight.extraction.kpi_extractor import ExtractedKPIs
from finsight.models.dcf import DCFModel, DCFAssumptions


@pytest.fixture
def kpis():
    return ExtractedKPIs(
        ticker="AAPL",
        period="FY2023",
        source_type="10-K",
        revenue=394_000.0,
        revenue_growth_yoy=0.02,
        gross_margin=0.441,
        operating_margin=0.298,
        net_margin=0.246,
        ebitda=120_000.0,
        eps_diluted=6.13,
        free_cash_flow=99_000.0,
        total_debt=95_000.0,
        cash_and_equivalents=62_000.0,
        shares_outstanding=15_500.0,
        key_risks=["Macro headwinds", "China exposure", "Regulatory risk"],
        growth_drivers=["AI integration", "Services growth", "Emerging markets"],
    )


@pytest.fixture
def dcf_result(kpis):
    model = DCFModel()
    return model.value_from_kpis(kpis)


@pytest.fixture
def generator():
    return ReportGenerator()


class TestReportGenerator:
    def test_generate_returns_equity_report(self, generator, kpis):
        report = generator.generate(ticker="AAPL", kpis=kpis)
        assert isinstance(report, EquityReport)

    def test_ticker_preserved(self, generator, kpis):
        report = generator.generate(ticker="aapl", kpis=kpis)
        assert report.ticker == "AAPL"

    def test_financial_summary_populated(self, generator, kpis):
        report = generator.generate(ticker="AAPL", kpis=kpis)
        assert report.financial_summary.get("revenue_m") == 394_000.0
        assert report.financial_summary.get("gross_margin") == pytest.approx(0.441)

    def test_dcf_summary_populated(self, generator, kpis, dcf_result):
        report = generator.generate(ticker="AAPL", kpis=kpis, dcf_result=dcf_result)
        assert "intrinsic_value_per_share" in report.dcf_summary
        assert report.model_value_per_share is not None

    # The gap is reported as what it is, with no rating or price-target language at any
    # distance from the price: on a generic 10% WACC a rule-based rating would be noise.
    _ADVICE = ("Rating", "Price Target", "| **Buy", "| **Sell", "| **Hold",
               "upside", "downside")

    def test_a_model_value_far_above_the_price_is_not_a_buy(self, generator, kpis, dcf_result):
        report = generator.generate(ticker="AAPL", kpis=kpis, dcf_result=dcf_result,
                                    current_price=50.0, splits=[])
        md = generator.to_markdown(report)
        assert report.model_vs_price > 0.15
        assert "above the price" in report.investment_thesis
        assert not [w for w in self._ADVICE if w in md]

    def test_a_model_value_far_below_the_price_is_not_a_sell(self, generator, kpis, dcf_result):
        report = generator.generate(ticker="AAPL", kpis=kpis, dcf_result=dcf_result,
                                    current_price=10_000.0, splits=[])
        md = generator.to_markdown(report)
        assert report.model_vs_price < -0.10
        assert "below the price" in report.investment_thesis
        assert not [w for w in self._ADVICE if w in md]

    def test_model_vs_price_is_the_plain_gap(self, generator, kpis, dcf_result):
        iv = round(dcf_result.intrinsic_value_per_share, 2)
        report = generator.generate(ticker="AAPL", kpis=kpis, dcf_result=dcf_result,
                                    current_price=iv * 1.05, splits=[])
        assert report.model_vs_price == pytest.approx(1 / 1.05 - 1)
        assert f"| {report.model_vs_price:+.1%} |" in generator.to_markdown(report)

    def test_to_markdown_returns_string(self, generator, kpis):
        report = generator.generate(ticker="AAPL", kpis=kpis)
        md = generator.to_markdown(report)
        assert isinstance(md, str)
        assert len(md) > 500

    def test_markdown_contains_ticker(self, generator, kpis):
        report = generator.generate(ticker="AAPL", kpis=kpis)
        md = generator.to_markdown(report)
        assert "AAPL" in md

    def test_markdown_labels_the_model_value_as_mechanical(self, generator, kpis, dcf_result):
        md = generator.to_markdown(generator.generate(
            ticker="AAPL", kpis=kpis, dcf_result=dcf_result, current_price=200.0))
        assert "A mechanical model output, not investment advice." in md
        assert "not a price target, a rating or a recommendation" in md
        # value_from_kpis without overrides: WACC and g are generic defaults.
        assert "10.0% WACC and 3.0% terminal growth, generic model defaults" in md
        assert "- **WACC:** 10.0% (model default, not company-specific)" in md

    def test_key_risks_populated(self, generator, kpis):
        report = generator.generate(ticker="AAPL", kpis=kpis)
        assert len(report.key_risks) == 3

    def test_no_kpis_still_generates(self, generator):
        report = generator.generate(ticker="XYZ")
        assert report.ticker == "XYZ"
        assert report.model_value_per_share is None
        assert report.model_vs_price is None

    def test_model_vs_price_calculation(self, generator):
        report = EquityReport(
            ticker="TEST", company_name="Test Co", report_date="2024-01-01",
            model_value_per_share=120.0, current_price=100.0
        )
        assert report.model_vs_price == pytest.approx(0.20, rel=1e-6)

    def test_model_vs_price_none_when_missing(self, generator):
        report = EquityReport(
            ticker="TEST", company_name="Test Co", report_date="2024-01-01"
        )
        assert report.model_vs_price is None

    def test_the_summary_keeps_sentences_capitalised(self, generator, kpis, dcf_result):
        """`". ".join(parts).capitalize()` lower-cased every later sentence
        ("dcf analysis indicates …")."""
        report = generator.generate(ticker="AAPL", kpis=kpis, dcf_result=dcf_result,
                                    current_price=10_000.0, splits=[])
        assert "The DCF model value" in report.investment_thesis
        assert "dcf" not in report.investment_thesis


class TestSyntheticSentimentStaysOutOfTheSummary:
    """The demo report makes no claim about a company's management from boilerplate text."""

    def _sentiment(self):
        from finsight.ingestion.transcripts import TranscriptFetcher
        from finsight.sentiment.analyzer import SentimentAnalyzer

        tr = TranscriptFetcher(cache=False).create_mock_transcript("TSLA", "Q4 (synthetic)")
        s = SentimentAnalyzer().analyze_transcript(tr)
        s.guidance_tone = "raised"
        return s

    def test_synthetic_tone_and_guidance_are_not_quoted(self, generator, kpis):
        sentiment = self._sentiment()
        # The placeholder reads Neutral; force a bullish label so this checks that
        # synthetic tone is not quoted.
        sentiment.overall.net_sentiment = 0.05
        assert sentiment.overall.sentiment_label == "Bullish"
        report = generator.generate(ticker="TSLA", kpis=kpis, sentiment=sentiment,
                                    sentiment_synthetic=True)
        assert "earnings call" not in report.investment_thesis
        assert "guidance" not in report.investment_thesis
        assert "Management raised forward guidance" not in report.key_highlights

    def test_a_real_transcript_is_quoted(self, generator, kpis):
        report = generator.generate(ticker="TSLA", kpis=kpis, sentiment=self._sentiment(),
                                    sentiment_synthetic=False)
        assert "Management raised guidance on that call." in report.investment_thesis
        assert "Management raised forward guidance" in report.key_highlights


def test_the_summary_states_figures_without_adjectives():
    """The summary is figures, with no adjective ("strong", "superior") that would be a view."""
    from types import SimpleNamespace
    from finsight.report.generator import ReportGenerator

    k = SimpleNamespace(period="FY2025", revenue=10_000.0, revenue_growth_yoy=0.25,
                        gross_margin=0.72, operating_margin=0.4, net_margin=0.3, ebitda=None,
                        eps_diluted=None, free_cash_flow=None, total_debt=None,
                        cash_and_equivalents=None, key_risks=[], growth_drivers=[])
    thesis = ReportGenerator().generate(ticker="X", kpis=k).investment_thesis
    assert thesis == "X: Revenue grew 25.0% year over year (FY2025). Gross margin was 72.0%."
    for word in ("strong", "superior", "momentum", "demonstrates"):
        assert word not in thesis.lower()


def test_comps_section_says_the_periods_differ():
    from finsight.models.comps import CompsAnalysis
    from finsight.report.generator import ReportGenerator
    from tests.test_comps import _StubMarketData

    from types import SimpleNamespace
    k = SimpleNamespace(period="FY2025", revenue=50_000.0, revenue_growth_yoy=0.1,
                        gross_margin=0.5, operating_margin=0.3, net_margin=0.2, ebitda=15_000.0,
                        eps_diluted=None, free_cash_flow=None, total_debt=None,
                        cash_and_equivalents=None, key_risks=[], growth_drivers=[],
                        net_income=10_000.0)
    comps = CompsAnalysis(_StubMarketData()).analyze("TGT", peers=["PEERA", "PEERB"], kpis=k)
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate(ticker="TGT", kpis=k, comps_result=comps))
    assert ("> **Periods differ.** Peer multiples are Yahoo Finance trailing-twelve-month "
            "(TTM) figures; they are applied to the target's FY2025 10-K revenue, EBITDA "
            "and net income") in md


def test_no_legal_name_is_invented():
    """Without a company name the report prints the ticker alone, no invented legal name."""
    from finsight.report.generator import ReportGenerator
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate(ticker="msft"))
    assert md.startswith("# MSFT\n") and "Inc." not in md


def test_the_report_states_the_per_share_basis():
    """NVDA FY2022's $94.13 is on its pre-split share count; the report did
    not say so."""
    from finsight.api import demo_data
    from finsight.models.dcf import DCFModel
    from finsight.report.generator import ReportGenerator

    k = demo_data._kpis_for("NVDA", "2022")
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate(ticker="NVDA", kpis=k, dcf_result=DCFModel().value_from_kpis(k)))
    assert ("- **Per-share basis:** shares as reported in the 10-K filed 2024-02-21 "
            "(0001045810-24-000029); not adjusted for any stock split after that filing") in md


# ---------------------------------------------------------------------------
# An unknown split history withholds model vs price
# ---------------------------------------------------------------------------

def _aapl_report(**kwargs):
    from finsight.api import demo_data
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFModel

    kpis = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
    gen = ReportGenerator()
    report = gen.generate("AAPL", kpis=kpis, dcf_result=DCFModel().value_from_kpis(kpis),
                          current_price=344.63, price_date=demo_data.PRICE_DATE, **kwargs)
    return report, gen.to_markdown(report)


def test_an_unknown_split_history_withholds_the_gap():
    """splits=None is unknown, so the report withholds the gap as the dashboard does."""
    report, md = _aapl_report(splits=None)
    assert report.model_vs_price is None
    assert "**Model vs price withheld:** the split history could not be retrieved" in md
    assert "-52.0%" not in md and "below the price" not in report.investment_thesis


def test_leaving_splits_out_is_unknown_too():
    report, _ = _aapl_report()
    assert report.model_vs_price is None and report.comparison_withheld


def test_no_splits_shows_the_gap():
    report, md = _aapl_report(splits=[])
    assert report.comparison_withheld is None
    assert report.model_vs_price == pytest.approx(-0.520, abs=0.001)
    assert "-52.0%" in md and "withheld" not in md


def test_with_no_price_there_is_nothing_to_withhold():
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFModel

    kpis = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate("AAPL", kpis=kpis, dcf_result=DCFModel().value_from_kpis(kpis)))
    assert "withheld" not in md


def test_the_demo_report_still_shows_its_gap():
    from finsight.api import demo_data

    md = demo_data.demo_report("AAPL")
    assert "-52.0%" in md and "withheld" not in md


# ---------------------------------------------------------------------------
# Enterprise value to per-share value, and what the inputs cannot show
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_the_report_reconciles_enterprise_value_to_the_per_share_value(ticker):
    """It prints net debt and shares, so enterprise value reconciles to the value."""
    from finsight.api import demo_data

    dcf = demo_data.demo_dcf(ticker)
    text = demo_data.demo_report(ticker)
    a = dcf["assumptions"]
    net_debt, shares = a["net_debt_m"], a["shares_outstanding_m"]
    money = f"{abs(net_debt) / 1e3:.1f}B"
    assert f"- **Net Debt:** {'-' if net_debt < 0 else ''}${money} (total debt − cash" in text
    assert f"- **Shares:** {shares:,.1f}M" in text
    assert f"- **Equity Value:** ${dcf['equity_value_m'] / 1e3:.1f}B" in text
    assert dcf["enterprise_value_m"] - net_debt == pytest.approx(dcf["equity_value_m"])
    assert dcf["equity_value_m"] / shares == pytest.approx(dcf["intrinsic_value_per_share"])


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_the_securities_exclusion_reaches_the_report_and_the_api(ticker):
    """The report states marketable securities are not netted."""
    from finsight.api import demo_data

    body = demo_data.demo_dcf(ticker)
    note = body["limitations"]["marketable_securities"]
    per_billion = 1_000 / body["assumptions"]["shares_outstanding_m"]
    assert f"each $1B not netted is ${per_billion:,.2f} a share" in note
    text = demo_data.demo_report(ticker)
    assert f"- DCF input limitation — marketable_securities: {note}" in text


def test_a_negative_net_debt_keeps_its_sign_before_the_currency():
    from finsight.api import demo_data

    assert "- **Net Debt:** -$9.9B (total debt" in demo_data.demo_report("TSLA")
    assert "$-" not in demo_data.demo_report("TSLA")


def test_the_readme_names_the_filer_the_exclusion_moves_most_from_repo_figures_only():
    from pathlib import Path

    from finsight.api import demo_data

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    for ticker, name in (("TSLA", "Tesla"), ("AAPL", "Apple")):
        d = demo_data.demo_dcf(ticker)
        per_share = 1_000 / d["assumptions"]["shares_outstanding_m"]
        value = d["intrinsic_value_per_share"]
        share = per_share / value
        shown = f"{share:.1%}" if share >= 0.01 else f"{share:.2%}"
        assert f"${per_share:,.2f} a share for {name} ({shown} of its ${value:,.2f})" in readme, name
    assert "(Apple, Alphabet)" not in readme


# ---------------------------------------------------------------------------
# A base year far behind the price says so
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("valuation, stale", [
    ("2025-04-30", False),   # exactly 548 days before 2026-10-30
    ("2025-04-29", True),    # 549
    ("2025-09-27", False),   # 398
    ("2014-09-27", True),
])
def test_a_valuation_date_over_the_threshold_is_stale(valuation, stale):
    from finsight.report.generator import STALE_VALUATION_DAYS, stale_valuation_note

    assert STALE_VALUATION_DAYS == 548
    note = stale_valuation_note(valuation, "2026-10-30")
    assert bool(note) is stale
    if stale:
        assert valuation in note and "2026-10-30" in note and "about 18 months" in note


@pytest.mark.parametrize("valuation, as_of", [
    (None, "2026-10-30"), ("2014-09-27", None), ("not a date", "2026-10-30")])
def test_an_unknown_date_is_not_called_stale(valuation, as_of):
    from finsight.report.generator import stale_valuation_note

    assert stale_valuation_note(valuation, as_of) is None


def _shifted_back(facts, years):
    """The filer's whole 10-K history, ``years`` earlier: its tags stop that much sooner."""
    def back(day):
        return f"{int(day[:4]) - years}{day[4:]}" if day else day
    facts = copy.deepcopy(facts)     # the snapshot is cached and shared
    for concept in facts["facts"]["us-gaap"].values():
        for rows in concept["units"].values():
            for row in rows:
                row.update(start=back(row.get("start")), end=back(row.get("end")),
                           filed=back(row.get("filed")))
                row["fy"] = row["fy"] - years if row.get("fy") else row.get("fy")
    return facts


def _live_app(monkeypatch, facts, closes_on):
    import importlib

    import pandas as pd

    import finsight.api.app as app_module
    import finsight.api.demo_data as demo_data

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    importlib.reload(demo_data)
    importlib.reload(app_module)
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000000001")
    monkeypatch.setattr(app_module._edgar, "get_company_facts", lambda cik: facts)
    monkeypatch.setattr(app_module._market, "get_company_profile",
                        lambda t: type("Profile", (), {"name": "Old Filer"})())
    monkeypatch.setattr(app_module._market, "get_splits", lambda t: [])
    monkeypatch.setattr(app_module._market, "get_peers", lambda t: [])
    monkeypatch.setattr(app_module._market, "get_price_history", lambda t, period: pd.DataFrame(
        {"Close": [100.0]}, index=pd.DatetimeIndex([closes_on])))
    return app_module


def test_a_filer_whose_tags_stopped_years_ago_is_told_on_the_dcf_and_the_report(monkeypatch):
    from fastapi.testclient import TestClient

    from finsight.data import companyfacts_snapshot

    facts = _shifted_back(companyfacts_snapshot("AAPL"), 11)
    client = TestClient(_live_app(monkeypatch, facts, "2026-10-30").app)

    dcf = client.get("/dcf/OLD").json()
    assert dcf["valuation_date"] == "2015-09-26"
    assert "days before the date of this request" in dcf["limitations"]["stale_valuation"]

    report = client.get("/report/OLD").text
    assert "> **Stale valuation.** The valuation date 2015-09-26 is 4,052 days before the " \
           "price date 2026-10-30" in report


def test_a_filer_at_the_threshold_is_not_told(monkeypatch):
    """The live /dcf reads the clock, so the side under the threshold is held to a
    valuation date made from today's."""
    from datetime import date, timedelta

    from finsight.api import payloads
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFModel

    kpis = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
    result = DCFModel().value_from_kpis(kpis)
    for days, stale in ((548, False), (549, True)):
        kpis.provenance["fiscal_year_end"] = (date.today() - timedelta(days=days)).isoformat()
        body = payloads.dcf_payload("AAPL", kpis, result, 5, mode="live")
        assert ("stale_valuation" in body["limitations"]) is stale, days


def test_no_bundled_latest_year_is_stale_and_the_demo_never_reads_the_clock(monkeypatch):
    from finsight.api import demo_data

    for ticker in demo_data.DEMO_TICKERS:
        body = demo_data.demo_dcf(ticker)
        assert "stale_valuation" not in body["limitations"], ticker
        assert "Stale valuation" not in demo_data.demo_report(ticker), ticker

    # Far past the bundled price date on any clock, and not the clock's to decide.
    class Later(datetime.date):
        @classmethod
        def today(cls):
            return cls(2040, 1, 1)
    monkeypatch.setattr("finsight.api.payloads.date", Later)
    assert "stale_valuation" not in demo_data.demo_dcf("AAPL")["limitations"]


def test_an_older_bundled_year_asked_for_says_so_and_keeps_its_value(monkeypatch):
    from finsight.api import demo_data

    body = demo_data.demo_dcf("AAPL", period="FY2022")
    assert "2022-09-24 is 1,497 days before the price date 2026-10-30" \
        in body["limitations"]["stale_valuation"]
    assert body["intrinsic_value_per_share"] > 0


def test_save_json_writes_null_for_a_non_finite_multiple(tmp_path):
    """A peer's infinite multiple is written as null: a literal Infinity is not JSON."""
    import json

    from finsight.report.generator import EquityReport, ReportGenerator

    report = EquityReport(ticker="X", company_name="X", report_date="2026-11-04")
    report.comps_summary = {"target_multiples": {"pe": float("inf"), "ps": 6.0},
                            "peer_median_multiples": {"pb": float("nan"), "p_fcf": -float("inf")},
                            "excluded_multiples": [{"value": float("inf"), "ticker": "NIO"}]}
    path = tmp_path / "report.json"
    ReportGenerator().save_json(report, str(path))

    def refuse(constant):
        raise AssertionError(f"{constant} is not JSON")

    saved = json.loads(path.read_text(encoding="utf-8"), parse_constant=refuse)
    assert saved["comps_summary"]["target_multiples"] == {"pe": None, "ps": 6.0}
    assert saved["comps_summary"]["peer_median_multiples"] == {"pb": None, "p_fcf": None}
    assert saved["comps_summary"]["excluded_multiples"] == [{"value": None, "ticker": "NIO"}]

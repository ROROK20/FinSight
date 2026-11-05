"""Tests for KPI extraction."""

import re

import pytest
from finsight.extraction.kpi_extractor import KPIExtractor, ExtractedKPIs


SAMPLE_10K_TEXT = """
Apple Inc. reported total revenue of $394.3 billion for fiscal year 2023,
representing a slight decline from $394.3 billion in the prior year.
Gross margin was 44.1%, while operating margin reached 29.8%.
Net income was $97.0 billion, with diluted EPS of $6.13.
Free cash flow was $99.6 billion.
The company held $61.6 billion in cash and equivalents.
Long-term debt stood at $95.3 billion.
R&D expenses were $29.9 billion.
"""

SAMPLE_TRANSCRIPT = """
CEO: We delivered revenue of $10.2 billion, up 15% year-over-year.
Our gross margin expanded to 68.3%.
We are raising our guidance for next quarter to $10.5 to $10.8 billion.
The primary risk remains macroeconomic uncertainty.
CFO: Free cash flow was $3.1 billion. EPS was $1.85.
"""


@pytest.fixture
def extractor():
    return KPIExtractor()


class TestKPIExtractor:
    def test_extract_returns_kpis_object(self, extractor):
        kpis = extractor.extract_from_text(
            SAMPLE_10K_TEXT, ticker="AAPL", period="FY2023", source_type="10-K"
        )
        assert isinstance(kpis, ExtractedKPIs)

    def test_extract_ticker_preserved(self, extractor):
        kpis = extractor.extract_from_text(SAMPLE_10K_TEXT, "AAPL", "FY2023")
        assert kpis.ticker == "AAPL"

    def test_regex_extracts_gross_margin(self, extractor):
        """Regex fallback should extract gross margin from text."""
        extractor._client = None  # force regex
        kpis = extractor.extract_from_text(SAMPLE_10K_TEXT, "AAPL", "FY2023")
        assert kpis.gross_margin == pytest.approx(0.441)

    def test_regex_extracts_revenue(self, extractor):
        extractor._client = None
        kpis = extractor.extract_from_text(SAMPLE_10K_TEXT, "AAPL", "FY2023")
        assert kpis.revenue == pytest.approx(394_300.0)

    def test_extraction_method_set(self, extractor):
        extractor._client = None
        kpis = extractor.extract_from_text(SAMPLE_10K_TEXT, "AAPL", "FY2023")
        assert kpis.extraction_method in ("llm", "regex")

    def test_extract_from_xbrl(self, extractor):
        """Test XBRL extraction with mock data."""
        xbrl_series = {
            "Revenues": [{"end": "2023-09-30", "val": 394_328_000_000, "form": "10-K", "filed": "2023-11-01"}],
            "NetIncomeLoss": [{"end": "2023-09-30", "val": 97_003_000_000, "form": "10-K", "filed": "2023-11-01"}],
            "GrossProfit": [{"end": "2023-09-30", "val": 169_148_000_000, "form": "10-K", "filed": "2023-11-01"}],
            "LongTermDebt": [{"end": "2023-09-30", "val": 95_281_000_000, "form": "10-K", "filed": "2023-11-01"}],
            "CashAndCashEquivalentsAtCarryingValue": [{"end": "2023-09-30", "val": 29_965_000_000, "form": "10-K", "filed": "2023-11-01"}],
        }
        kpis = extractor.extract_from_xbrl(xbrl_series, "AAPL", "2023")
        assert kpis.revenue == pytest.approx(394328.0, rel=1e-3)
        assert kpis.gross_margin == pytest.approx(169_148 / 394_328, rel=1e-3)

    def test_xbrl_computes_margins(self, extractor):
        xbrl_series = {
            "Revenues": [{"end": "2023-12-31", "val": 1_000_000_000, "form": "10-K", "filed": "2024-02-01"}],
            "GrossProfit": [{"end": "2023-12-31", "val": 600_000_000, "form": "10-K", "filed": "2024-02-01"}],
            "NetIncomeLoss": [{"end": "2023-12-31", "val": 200_000_000, "form": "10-K", "filed": "2024-02-01"}],
        }
        kpis = extractor.extract_from_xbrl(xbrl_series, "TEST", "2023")
        assert kpis.gross_margin == pytest.approx(0.60, rel=1e-3)
        assert kpis.net_margin == pytest.approx(0.20, rel=1e-3)

    def test_to_dict_returns_dict(self, extractor):
        extractor._client = None
        kpis = extractor.extract_from_text(SAMPLE_10K_TEXT, "AAPL", "FY2023")
        d = kpis.to_dict()
        assert isinstance(d, dict)
        assert "ticker" in d
        assert "revenue" in d


class TestExtractedKPIs:
    def test_default_values(self):
        kpis = ExtractedKPIs(ticker="TEST", period="FY2023", source_type="10-K")
        assert kpis.revenue is None
        assert kpis.key_risks == []
        assert kpis.growth_drivers == []

    def test_repr(self):
        kpis = ExtractedKPIs(ticker="AAPL", period="FY2023", source_type="10-K",
                             revenue=394000, net_margin=0.25)
        assert "AAPL" in repr(kpis)


# ---------------------------------------------------------------------------
# XBRL: one fiscal year, never mixed with another, with provenance
# ---------------------------------------------------------------------------

def _fact(end, val, start=None, filed="2025-10-31", fy=2025, accn="0000320193-25-000079"):
    e = {"end": end, "val": val, "form": "10-K", "filed": filed, "fy": fy, "accn": accn}
    if start:
        e["start"] = start
    return e


def _facts(**concepts):
    """companyfacts-shaped payload: concept=(unit, [entries])."""
    return {"facts": {"us-gaap": {
        c: {"units": {unit: entries}} for c, (unit, entries) in concepts.items()
    }}}


class TestXBRLFiscalYear:
    """Regression pins for the stale-data defect: `/kpis` reported a 2009
    `Revenues` figure under a 2025 period, because a concept with no value for
    the target year fell back to its most recent entry from ANY year."""

    def test_period_never_borrows_another_years_value(self, extractor):
        """Through extract_from_xbrl(series, ticker, "YYYY"): FY2025 must not read the
        2009 `Revenues` figure or the FY2024 net income."""
        series = {
            "Revenues": [{"end": "2009-09-26", "val": 42_905_000_000,
                          "form": "10-K", "filed": "2009-10-27"}],
            "RevenueFromContractWithCustomerExcludingAssessedTax": [
                {"end": "2025-09-27", "val": 416_161_000_000,
                 "form": "10-K", "filed": "2025-10-31"}],
            "NetIncomeLoss": [{"end": "2024-09-28", "val": 93_736_000_000,
                               "form": "10-K", "filed": "2024-11-01"}],
        }
        kpis = extractor.extract_from_xbrl(series, "AAPL", "2025")
        assert kpis.revenue == pytest.approx(416_161.0)
        assert kpis.net_income is None

    def test_a_stale_concept_never_supplies_the_current_year(self, extractor):
        facts = _facts(
            Revenues=("USD", [_fact("2009-09-26", 42_905_000_000, "2008-09-28",
                                    filed="2009-10-27", fy=2009)]),
            RevenueFromContractWithCustomerExcludingAssessedTax=("USD", [
                _fact("2024-09-28", 391_035_000_000, "2023-10-01", filed="2024-11-01", fy=2024),
                _fact("2025-09-27", 416_161_000_000, "2024-09-29"),
            ]),
        )
        kpis = extractor.extract_from_companyfacts(facts, "AAPL")
        assert kpis.revenue == pytest.approx(416_161.0)
        assert kpis.period == "FY2025"
        assert kpis.provenance["fiscal_year_end"] == "2025-09-27"
        assert kpis.provenance["concepts"]["revenue"] == (
            "RevenueFromContractWithCustomerExcludingAssessedTax")
        assert kpis.revenue_growth_yoy == pytest.approx(416_161 / 391_035 - 1)

    def test_a_concept_missing_for_the_year_is_left_empty_not_borrowed(self, extractor):
        facts = _facts(
            RevenueFromContractWithCustomerExcludingAssessedTax=("USD", [
                _fact("2025-09-27", 416_161_000_000, "2024-09-29")]),
            NetIncomeLoss=("USD", [_fact("2024-09-28", 93_736_000_000, "2023-10-01",
                                         filed="2024-11-01", fy=2024)]),
        )
        kpis = extractor.extract_from_companyfacts(facts, "AAPL")
        assert kpis.net_income is None
        assert "net_income" in kpis.provenance["missing"]
        assert kpis.net_margin is None

    def test_a_requested_year_that_is_not_reported_is_an_error(self, extractor):
        facts = _facts(RevenueFromContractWithCustomerExcludingAssessedTax=("USD", [
            _fact("2025-09-27", 416_161_000_000, "2024-09-29")]))
        with pytest.raises(ValueError, match="fiscal year 2019"):
            extractor.extract_from_companyfacts(facts, "AAPL", period="2019")

    def test_no_revenue_at_all_is_an_error(self, extractor):
        with pytest.raises(ValueError, match="No annual revenue"):
            extractor.extract_from_companyfacts(_facts(), "ZZZZ")

    def test_capex_alone_never_becomes_free_cash_flow(self, extractor):
        """Accrued capex is a non-cash disclosure, not capex, and FCF needs a real capex: CFO alone would overstate it."""
        facts = _facts(
            RevenueFromContractWithCustomerExcludingAssessedTax=("USD", [
                _fact("2025-09-27", 416_161_000_000, "2024-09-29")]),
            NetCashProvidedByUsedInOperatingActivities=("USD", [
                _fact("2025-09-27", 111_482_000_000, "2024-09-29")]),
            CapitalExpendituresIncurredButNotYetPaid=("USD", [
                _fact("2025-09-27", 2_000_000_000, "2024-09-29")]),
        )
        kpis = extractor.extract_from_companyfacts(facts, "AAPL")
        assert kpis.capex is None
        assert kpis.free_cash_flow is None
        assert {"capex", "free_cash_flow"} <= set(kpis.provenance["missing"])


class TestRealCompanyFacts:
    """The bundled snapshots are real SEC companyfacts (retrieved 2026-11-04),
    so these assertions are checkable against the cited 10-K."""

    def test_growth_needs_the_year_right_before_it(self, extractor):
        """A missing year must leave growth blank: two years' change is not YoY."""
        import copy
        from finsight.data import companyfacts_snapshot

        facts = copy.deepcopy(companyfacts_snapshot("AAPL"))
        concept = "RevenueFromContractWithCustomerExcludingAssessedTax"
        unit = facts["facts"]["us-gaap"][concept]["units"]
        kept = [r for r in unit["USD"] if r["end"] != "2025-09-27"]
        assert len(kept) < len(unit["USD"]) and any(r["end"] == "2024-09-28" for r in kept)
        unit["USD"] = kept

        kpis = extractor.extract_from_companyfacts(facts, "AAPL")
        assert kpis.period == "FY2026" and kpis.revenue == pytest.approx(478_834.0)
        assert kpis.revenue_growth_yoy is None

    def test_growth_reads_the_adjacent_year(self, extractor):
        from finsight.data import companyfacts_snapshot

        kpis = extractor.extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
        assert kpis.revenue_growth_yoy == pytest.approx(478_834 / 416_161 - 1)

    def test_apple_fy2026_from_its_10k(self, extractor):
        from finsight.data import companyfacts_snapshot

        kpis = extractor.extract_from_companyfacts(companyfacts_snapshot("AAPL"), "AAPL")
        p = kpis.provenance
        assert (kpis.period, p["fiscal_year_end"], p["accession"]) == (
            "FY2026", "2026-09-26", "0000320193-26-000026")
        assert kpis.revenue == pytest.approx(478_834.0)          # ASC 606 concept
        assert kpis.shares_outstanding == pytest.approx(14_720.306)  # units.shares
        assert kpis.eps_diluted == pytest.approx(8.98)               # units USD/shares
        assert kpis.ebitda == pytest.approx(158_063 + 13_423)        # OI + D&A
        assert kpis.free_cash_flow == pytest.approx(145_097 - 9_041)
        assert kpis.total_debt == pytest.approx(78_114 + 1_997)      # term debt + CP
        assert p["missing"] == []

    def test_concept_chains_cover_filers_that_tag_differently(self, extractor):
        from finsight.data import companyfacts_snapshot

        nvda = extractor.extract_from_companyfacts(companyfacts_snapshot("NVDA"), "NVDA")
        assert nvda.period == "FY2026"
        assert nvda.provenance["concepts"]["revenue"] == "Revenues"
        assert nvda.provenance["concepts"]["capex"] == "PaymentsToAcquireProductiveAssets"
        googl = extractor.extract_from_companyfacts(companyfacts_snapshot("GOOGL"), "GOOGL")
        assert googl.gross_profit == pytest.approx(402_836 - 162_535)  # no GrossProfit tag

    def test_an_earlier_fiscal_year_can_be_requested_by_label(self, extractor):
        from finsight.data import companyfacts_snapshot

        kpis = extractor.extract_from_companyfacts(
            companyfacts_snapshot("AAPL"), "AAPL", period="2023")
        assert kpis.period == "FY2023"
        assert kpis.provenance["fiscal_year_end"] == "2023-09-30"
        assert kpis.revenue == pytest.approx(383_285.0)
        assert kpis.revenue_growth_yoy == pytest.approx(383_285 / 394_328 - 1)


# ---------------------------------------------------------------------------
# D&A: total concept first, then depreciation + amortization, and a
# depreciation-only figure is flagged
# ---------------------------------------------------------------------------

def _series(**vals):
    """Minimal XBRL series at one fiscal year-end ($ raw)."""
    end = "2026-06-30"
    out = {"RevenueFromContractWithCustomerExcludingAssessedTax":
           [{"end": end, "val": 100e9, "unit": "USD", "accn": "A-1", "filed": "2026-07-30",
             "first_accn": "A-1", "first_filed": "2026-07-30", "first_fy": 2026}],
           "OperatingIncomeLoss": [{"end": end, "val": 40e9, "unit": "USD", "accn": "A-1",
                                    "filed": "2026-07-30"}]}
    for concept, v in vals.items():
        out[concept] = [{"end": end, "val": v, "unit": "USD", "accn": "A-1", "filed": "2026-07-30"}]
    return out


class TestDepreciationAndAmortization:
    def test_the_total_concept_wins_over_depreciation_alone(self):
        k = KPIExtractor().extract_from_xbrl(
            _series(DepreciationDepletionAndAmortization=12e9, Depreciation=9e9), "X")
        assert k.ebitda == pytest.approx(52_000)
        assert k.provenance["concepts"]["ebitda"] == (
            "OperatingIncomeLoss + DepreciationDepletionAndAmortization (derived)")
        assert "depreciation_amortization" not in k.provenance["limitations"]

    def test_depreciation_plus_amortization_when_no_total_is_reported(self):
        k = KPIExtractor().extract_from_xbrl(
            _series(Depreciation=9e9, AmortizationOfIntangibleAssets=2e9), "X")
        assert k.ebitda == pytest.approx(51_000)
        assert k.provenance["concepts"]["ebitda"] == (
            "OperatingIncomeLoss + (Depreciation + AmortizationOfIntangibleAssets) (derived)")
        assert "depreciation_amortization" not in k.provenance["limitations"]

    def test_depreciation_alone_is_flagged_as_understated(self):
        """MSFT, GOOGL and TSLA: where D&A is depreciation only, the response says so."""
        k = KPIExtractor().extract_from_xbrl(_series(Depreciation=9e9), "X")
        assert k.ebitda == pytest.approx(49_000)
        note = k.provenance["limitations"]["depreciation_amortization"]
        assert note.startswith("D&A is us-gaap:Depreciation — depreciation of property "
                               "and equipment only")
        # Live data (no snapshot): the absences are the filer's (wording
        # widened 2026-11-02 to tell this apart from a snapshot gap).
        assert "The filer's 10-K facts for this fiscal year report no total D&A concept" in note
        assert "nor AmortizationOfIntangibleAssets" in note
        assert note.endswith("D&A and EBITDA exclude any amortization and are understated.")

    def test_finance_leases_are_always_disclosed(self):
        k = KPIExtractor().extract_from_xbrl(
            _series(PaymentsToAcquirePropertyPlantAndEquipment=5e9), "X")
        assert k.provenance["limitations"]["finance_leases"] == (
            "CapEx is cash paid for property and equipment "
            "(PaymentsToAcquirePropertyPlantAndEquipment); assets obtained under "
            "finance leases are not included, and finance-lease liabilities are not "
            "in total debt.")


@pytest.mark.parametrize("ticker,concept", [
    ("AAPL", "DepreciationDepletionAndAmortization"), ("NVDA", "DepreciationDepletionAndAmortization"),
    ("MSFT", "(Depreciation + AmortizationOfIntangibleAssets)"),
    ("GOOGL", "Depreciation"), ("TSLA", "Depreciation")])
def test_demo_dcf_names_its_da_concept_and_limitations(ticker, concept):
    from finsight.api import demo_data

    dcf = demo_data.demo_dcf(ticker)
    assert dcf["assumption_sources"]["da_as_pct_revenue"].endswith(f"— D&A from {concept}")
    assert ("depreciation_amortization" in dcf["limitations"]) is (concept == "Depreciation")
    assert "finance_leases" in dcf["limitations"]
    report = demo_data.demo_report(ticker)
    assert "- DCF input limitation — finance_leases: CapEx is cash paid" in report


# ---------------------------------------------------------------------------
# Regex fallback units
# ---------------------------------------------------------------------------

class TestRegexFallbackUnits:
    """"$94.9B" reads as $94.9B: every unit is honoured, not only the word "billion"."""

    @pytest.mark.parametrize("text,revenue_m", [
        ("Total revenue was $94.9B.", 94_900.0),
        ("Total revenue was $94.9 billion.", 94_900.0),
        ("Net sales were $5.2bn.", 5_200.0),
        ("Revenue reached $2.1 trillion.", 2_100_000.0),
        ("Revenue reached $2.1T.", 2_100_000.0),
        ("Revenue was $520 million.", 520.0),
        ("Revenue was $520M.", 520.0),
        ("Revenue was $310mm.", 310.0),
        ("Revenue was $750K.", 0.75),
        ("Revenue was $750 thousand.", 0.75),
        ("Revenue was $1,234.", 1_234.0),   # no unit: taken as $M, never guessed
    ])
    def test_every_unit_is_applied(self, text, revenue_m):
        k = KPIExtractor().extract_from_text(text, ticker="X", period="Q")
        assert k.revenue == pytest.approx(revenue_m)

    def test_the_reviewers_sentence(self):
        k = KPIExtractor().extract_from_text(
            "Total revenue was $94.9B and net income was $15.3B.", ticker="X", period="Q")
        assert (k.revenue, k.net_income) == (pytest.approx(94_900.0), pytest.approx(15_300.0))

    def test_a_word_after_the_number_is_not_a_unit(self):
        k = KPIExtractor().extract_from_text("Revenue was $12 more than planned.", ticker="X", period="Q")
        assert k.revenue == pytest.approx(12.0)

    def test_the_readme_states_the_fallbacks_real_coverage(self):
        from pathlib import Path
        from finsight.extraction import kpi_extractor as ke

        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        llm_fields = len(re.findall(r"^- \w+ \(", ke._KPI_EXTRACTION_PROMPT, re.M))
        assert llm_fields == 22 and len(ke.REGEX_FIELDS) == 6
        assert "asks the LLM for 22 fields" in readme
        assert "regex fallback that reads only 6" in readme
        assert "20+ metrics" not in readme


# ---------------------------------------------------------------------------
# Comparative years in a company's FIRST XBRL 10-K keep their own labels
# ---------------------------------------------------------------------------

def _first_xbrl_10k_facts(fy_offset=0, ends=("2019-12-31", "2020-12-31", "2021-12-31")):
    """Its first 10-K (fy 2021 + offset) reports three years; a later one
    (fy 2022 + offset) adds the next and re-reports two."""
    rows = []
    for end, val in zip(ends, (100e9, 110e9, 121e9)):
        start = f"{int(end[:4]) - 1}{end[4:]}"
        rows.append({"start": start, "end": end, "val": val, "form": "10-K", "fy": 2021 + fy_offset,
                     "fp": "FY", "accn": "F-1", "filed": "2022-02-15"})
    for end, val in ((ends[1], 110e9), (ends[2], 121e9), ("2022-12-31" if ends[2].startswith("2021-12") else "2023-01-28", 133e9)):
        start = f"{int(end[:4]) - 1}{end[4:]}"
        rows.append({"start": start, "end": end, "val": val, "form": "10-K", "fy": 2022 + fy_offset,
                     "fp": "FY", "accn": "F-2", "filed": "2023-02-15"})
    return {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": rows}},
    }}}


class TestFirstXbrlTenK:
    """The prior years in a first XBRL 10-K keep their own fiscal year: period=2020 is served, 2020-12-31 is FY2020."""

    def test_each_year_is_labelled_by_its_own_end(self):
        k = KPIExtractor().extract_from_companyfacts(_first_xbrl_10k_facts(), "NEWCO", "2020")
        assert k.period == "FY2020" and k.revenue == pytest.approx(110_000)
        assert k.provenance["fiscal_year_end"] == "2020-12-31"

    def test_an_exact_year_end_gets_its_own_label(self):
        k = KPIExtractor().extract_from_companyfacts(_first_xbrl_10k_facts(), "NEWCO", "2020-12-31")
        assert k.period == "FY2020"

    def test_the_error_lists_distinct_years(self):
        with pytest.raises(ValueError) as e:
            KPIExtractor().extract_from_companyfacts(_first_xbrl_10k_facts(), "NEWCO", "2015")
        assert "FY2019, FY2020, FY2021, FY2022" in str(e.value)

    def test_a_january_year_end_filer_keeps_its_own_convention(self):
        """A retailer whose fiscal 2021 ended 2022-01-29 (fy one behind the end year)."""
        facts = _first_xbrl_10k_facts(fy_offset=0, ends=("2020-02-01", "2021-01-30", "2022-01-29"))
        k = KPIExtractor().extract_from_companyfacts(facts, "RTLR", "2020")
        assert k.provenance["fiscal_year_end"] == "2021-01-30"


# ---------------------------------------------------------------------------
# LLM guidance: annual only, explicit period, coerced types (no network)
# ---------------------------------------------------------------------------

class _StubLLM:
    """Stands in for the OpenAI client: returns a canned JSON answer."""

    def __init__(self, answer: dict):
        import json
        from types import SimpleNamespace
        content = json.dumps(answer)
        create = lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _llm_kpis(answer: dict):
    x = KPIExtractor()
    x._client = _StubLLM(answer)
    return x.extract_from_text("text", ticker="AAPL", period="FY2025")


_AAPL_LIKE = {"revenue": 416161, "operating_income": 133050, "ebitda": 144748,
              "shares_outstanding": 15004.697, "revenue_growth_yoy": 0.0643,
              "capex": 12715, "total_debt": 98657, "cash_and_equivalents": 35934}


class TestLLMGuidance:
    def test_quarterly_guidance_is_not_used_as_annual(self):
        """Next-quarter guidance ($10.5–10.8B) against annual revenue must not read as −73.6% growth."""
        from finsight.models.dcf import DCFModel
        k = _llm_kpis({**_AAPL_LIKE, "guidance_revenue_low": 10_500, "guidance_revenue_high": 10_800,
                       "guidance_period": "quarterly"})
        a = DCFModel()._infer_assumptions(k)
        assert a.revenue_growth_rates[0] == pytest.approx(0.0643)
        assert a.sources["revenue_growth_rates"].endswith(
            "revenue guidance ignored (not stated as full-year)")

    def test_guidance_with_no_period_is_not_used_either(self):
        from finsight.models.dcf import DCFModel
        k = _llm_kpis({**_AAPL_LIKE, "guidance_revenue_low": 10_500, "guidance_revenue_high": 10_800})
        assert k.guidance_period is None
        assert DCFModel()._infer_assumptions(k).revenue_growth_rates[0] == pytest.approx(0.0643)

    def test_annual_guidance_is_used(self):
        from finsight.models.dcf import DCFModel
        k = _llm_kpis({**_AAPL_LIKE, "guidance_revenue_low": 430_000, "guidance_revenue_high": 440_000,
                       "guidance_period": "Annual"})
        a = DCFModel()._infer_assumptions(k)
        assert a.revenue_growth_rates[0] == pytest.approx(435_000 / 416_161 - 1)
        assert a.sources["revenue_growth_rates"].startswith("full-year revenue guidance midpoint")

    def test_string_values_are_coerced_not_crashed_on(self):
        """String values from the LLM crashed the DCF with a TypeError."""
        from finsight.models.dcf import DCFModel
        k = _llm_kpis({**_AAPL_LIKE, "revenue": "$416.2B", "gross_margin": "46.9%",
                       "eps_diluted": "7.46", "capex": "12,715", "net_income": "n/a",
                       "key_risks": "not a list", "guidance_period": "next quarter"})
        assert k.revenue == pytest.approx(416_200.0)
        assert k.gross_margin == pytest.approx(0.469)
        assert k.eps_diluted == pytest.approx(7.46) and k.capex == pytest.approx(12_715.0)
        assert k.net_income is None and k.key_risks == [] and k.guidance_period is None
        assert DCFModel().value_from_kpis(k).intrinsic_value_per_share > 0


def test_no_unmeasured_confidence_number_ships():
    """No `confidence` number: nothing measures one, so it must not ship. `extraction_method` says how a figure was read."""
    from dataclasses import fields
    from fastapi.testclient import TestClient
    from finsight.api import app as app_module
    from finsight.extraction.kpi_extractor import ExtractedKPIs, KPIExtractor

    assert "confidence" not in {f.name for f in fields(ExtractedKPIs)}
    k = KPIExtractor()._dict_to_kpis({"revenue": 100.0, "confidence": 0.99}, "X", "FY2025", "10-K", "llm")
    assert not hasattr(k, "confidence") and "confidence" not in k.to_dict()
    assert k.revenue == 100.0

    os_env = pytest.MonkeyPatch()
    os_env.setenv("FINSIGHT_DEMO", "1")
    try:
        body = TestClient(app_module.app).get("/kpis/AAPL").json()
    finally:
        os_env.undo()
    assert "confidence" not in body
    assert body["extraction_method"] == "xbrl"

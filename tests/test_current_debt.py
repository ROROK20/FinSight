"""
Whether LongTermDebt holds current debt.

True of Apple, Microsoft, NVIDIA and Alphabet, not of Tesla, whose tag is the noncurrent
part only: its FY2025 10-K shows $1,569M current beside $6,584M long-term at 2025-12-31.
Current debt is added on evidence and only on evidence, and when nothing settles it the
response says so.
"""

import pytest

from finsight.extraction.kpi_extractor import KPIExtractor

END = "2024-12-31"
# Tesla's FY2024 figures under the us-gaap balance-sheet tags. Its real sheet lines are
# tsla: extension tags, outside companyfacts' us-gaap, so this is the shape of a filer that
# uses these tags, and the evidence rule at work: the real Tesla stays unsettled.
TSLA_FY2024 = {"LongTermDebt": 5_535, "DebtCurrent": 2_343,
               "LongTermDebtAndCapitalLeaseObligationsCurrent": 2_456,
               "LongTermDebtAndCapitalLeaseObligations": 5_757,
               "FinanceLeaseLiabilityCurrent": 113, "FinanceLeaseLiabilityNoncurrent": 222}


def _extract(debt_tags, snapshot=None):
    """One fiscal year whose debt concepts are ``debt_tags`` ($M)."""
    rows = {"Revenues": 10_000, **debt_tags}
    series = {c: [{"end": END, "val": v * 1_000_000, "unit": "USD", "fy": 2024, "form": "10-K",
                   "accn": "0000000000-25-000001", "filed": "2025-01-30",
                   "first_fy": 2024, "first_accn": "0000000000-25-000001", "first_filed": "2025-01-30"}]
              for c, v in rows.items()}
    return KPIExtractor().extract_from_xbrl(series, "X", snapshot=snapshot)


def _debt(debt_tags, snapshot=None):
    k = _extract(debt_tags, snapshot)
    return k.total_debt, k.provenance["concepts"].get("total_debt"), k.provenance["limitations"].get("current_debt")


# --- evidence says it is excluded: current debt is added ------------------------

def test_a_long_term_debt_equal_to_the_noncurrent_tag_excludes_current():
    total, concept, note = _debt({"LongTermDebt": 5_000, "LongTermDebtNoncurrent": 5_000,
                                  "LongTermDebtCurrent": 1_000})
    assert total == 6_000
    assert concept == "LongTermDebt + LongTermDebtCurrent (derived)"
    assert note is None


def test_the_tesla_shape_reconciles_with_the_balance_sheet():
    """$5,535M + $2,343M = $7,878M, the sheet's $2,456M + $5,757M less $335M of leases."""
    total, concept, note = _debt(TSLA_FY2024)
    assert total == 7_878
    assert concept == "LongTermDebt + DebtCurrent (derived)"
    assert note is None


def test_debt_current_is_never_added_to_the_short_term_borrowings_inside_it():
    total, concept, _ = _debt({**TSLA_FY2024, "CommercialPaper": 500})
    assert total == 7_878
    assert "CommercialPaper" not in concept


def test_commercial_paper_is_still_added_to_the_current_portion_of_long_term_debt():
    total, concept, _ = _debt({"LongTermDebt": 5_000, "LongTermDebtNoncurrent": 5_000,
                               "LongTermDebtCurrent": 1_000, "CommercialPaper": 500})
    assert total == 6_500
    assert concept == "LongTermDebt + LongTermDebtCurrent + CommercialPaper (derived)"


def test_the_sheet_line_less_its_tagged_leases_is_current_debt_when_nothing_narrower_is_tagged():
    tags = {k: v for k, v in TSLA_FY2024.items() if k != "DebtCurrent"}
    total, concept, _ = _debt(tags)
    assert total == 7_878
    assert concept == ("LongTermDebt + (LongTermDebtAndCapitalLeaseObligationsCurrent − "
                       "FinanceLeaseLiabilityCurrent) (derived)")
    assert set(_extract(tags).provenance["figure_filings"]) >= {
        "LongTermDebtAndCapitalLeaseObligationsCurrent", "FinanceLeaseLiabilityCurrent"}


def test_a_sheet_line_with_its_leases_inside_says_so():
    total, concept, note = _debt({"LongTermDebt": 5_000,
                                  "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000,
                                  "LongTermDebtAndCapitalLeaseObligations": 5_000})
    assert total == 6_000
    assert concept == "LongTermDebt + LongTermDebtAndCapitalLeaseObligationsCurrent (derived)"
    assert "finance leases are counted" in note


# --- evidence says it is included, or nothing settles it: never counted twice ----

def test_a_long_term_debt_equal_to_noncurrent_plus_current_is_left_alone():
    total, concept, note = _debt({"LongTermDebt": 6_000, "LongTermDebtNoncurrent": 5_000,
                                  "LongTermDebtCurrent": 1_000})
    assert (total, concept, note) == (6_000, "LongTermDebt", None)


def test_a_long_term_debt_above_the_noncurrent_tag_is_left_alone():
    """Alphabet's FY2025: LongTermDebt is face value, 1% above noncurrent + current."""
    total, concept, note = _debt({"LongTermDebt": 49_085, "LongTermDebtNoncurrent": 46_547,
                                  "LongTermDebtCurrent": 1_996})
    assert (total, concept, note) == (49_085, "LongTermDebt", None)


def test_a_reported_zero_of_current_debt_settles_it():
    """NVIDIA's FY2025: LongTermDebt equals noncurrent because nothing is current."""
    assert _debt({"LongTermDebt": 8_463, "LongTermDebtNoncurrent": 8_463,
                  "LongTermDebtCurrent": 0}) == (8_463, "LongTermDebt", None)
    assert _debt({"LongTermDebt": 15_440, "LongTermDebtCurrent": 0}) == (15_440, "LongTermDebt", None)


def test_current_debt_that_rounding_could_explain_is_not_added():
    total, _, _ = _debt({"LongTermDebt": 10_000, "LongTermDebtNoncurrent": 10_000,
                         "LongTermDebtCurrent": 50})
    assert total == 10_000


def test_a_sheet_that_shows_long_term_debt_is_the_total_stops_the_addition():
    total, concept, note = _debt({"LongTermDebt": 6_000, "DebtCurrent": 1_000,
                                  "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000,
                                  "LongTermDebtAndCapitalLeaseObligations": 5_000})
    assert (total, concept, note) == (6_000, "LongTermDebt", None)


def test_current_debt_beside_long_term_debt_with_no_evidence_is_not_added_and_says_so():
    total, concept, note = _debt({"LongTermDebt": 5_000, "DebtCurrent": 1_000})
    assert (total, concept) == (5_000, "LongTermDebt")
    assert "DebtCurrent ($1,000M) is reported" in note and "it is not added" in note
    assert "If it does not, net debt is understated" in note
    assert "per-share value overstated" in note


def test_a_sheet_that_reconciles_neither_way_is_not_evidence():
    total, _, note = _debt({"LongTermDebt": 5_000, "DebtCurrent": 1_000,
                            "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_500,
                            "LongTermDebtAndCapitalLeaseObligations": 6_500})
    assert total == 5_000 and "it is not added" in note


# --- no current figure at all: said, not silent ---------------------------------

SNAPSHOT = {"retrieved": "2026-10-31", "not_carried": ["DebtCurrent",
            "LongTermDebtAndCapitalLeaseObligationsCurrent"]}


def test_a_snapshot_without_a_current_debt_concept_says_so():
    total, concept, note = _debt({"LongTermDebt": 5_000}, snapshot=SNAPSHOT)
    assert (total, concept) == (5_000, "LongTermDebt")
    assert "current debt not in this snapshot" in note
    assert "unknown until a refresh" in note


def test_a_filer_that_reports_no_current_debt_concept_is_not_blamed_on_a_snapshot():
    _, _, note = _debt({"LongTermDebt": 5_000})
    assert "not in this snapshot" not in note
    assert "No current-debt concept" in note and "is reported for 2024-12-31" in note


def test_a_noncurrent_figure_alone_says_current_debt_is_missing():
    total, concept, note = _debt({"LongTermDebtNoncurrent": 5_000})
    assert (total, concept) == (5_000, "LongTermDebtNoncurrent")
    assert "may leave current debt out" in note


def test_only_current_debt_says_noncurrent_is_missing():
    total, _, note = _debt({"LongTermDebtCurrent": 1_000})
    assert total == 1_000 and "Only current debt is reported" in note


def test_no_debt_concept_at_all_is_not_this_notes_business():
    k = _extract({})
    assert k.total_debt is None and "current_debt" not in k.provenance["limitations"]
    assert "total_debt" in k.provenance["missing"]


def test_the_two_parts_without_long_term_debt_still_add_up():
    total, concept, note = _debt({"LongTermDebtNoncurrent": 109_106, "LongTermDebtCurrent": 9_613,
                                  "CommercialPaper": 6_000})
    assert total == 124_719 and note is None
    assert concept == "LongTermDebtNoncurrent + LongTermDebtCurrent + CommercialPaper (derived)"


# --- the evidence concepts do not leak into the cited figures -------------------

def test_a_concept_read_as_evidence_is_not_cited_as_a_source():
    k = _extract({"LongTermDebt": 6_000, "LongTermDebtNoncurrent": 5_000, "LongTermDebtCurrent": 1_000})
    assert set(k.provenance["figure_filings"]) >= {"LongTermDebt"}
    assert "LongTermDebtCurrent" not in k.provenance["figure_filings"]
    assert "LongTermDebtNoncurrent" not in k.provenance["figure_filings"]
    assert not {"long_term_debt", "long_term_debt_noncurrent", "long_term_debt_current", "debt_current"} \
        & set(k.provenance["concepts"])


# --- the bundled snapshots ------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL"])
def test_the_four_filers_that_reconcile_carry_no_note_in_any_year(ticker):
    from finsight.data import companyfacts_snapshot

    facts = companyfacts_snapshot(ticker)
    gaap = facts["facts"]["us-gaap"]
    ends = {e["end"] for c in ("LongTermDebt", "LongTermDebtNoncurrent")
            for rows in gaap[c]["units"].values() for e in rows}
    seen = 0
    for end in sorted(ends):
        k = KPIExtractor().extract_from_companyfacts(facts, ticker, end)
        seen += 1
        assert "current_debt" not in k.provenance["limitations"], k.period
        assert k.provenance["concepts"]["total_debt"].split(" + ")[0] in (
            "LongTermDebt", "LongTermDebtNoncurrent"), k.period
    assert seen >= 4


@pytest.mark.parametrize("ticker, debt, concept", [
    ("AAPL", 80_111, "LongTermDebt + CommercialPaper (derived)"),
    ("MSFT", 40_294, "LongTermDebt"),
    ("NVDA", 8_468, "LongTermDebt"),
    ("GOOGL", 49_085, "LongTermDebt + CommercialPaper (derived)"),
    ("TSLA", 6_584, "LongTermDebt"),
])
def test_no_bundled_debt_figure_moves(ticker, debt, concept):
    from finsight.data import companyfacts_snapshot

    k = KPIExtractor().extract_from_companyfacts(companyfacts_snapshot(ticker), ticker)
    assert k.total_debt == debt
    assert k.provenance["concepts"]["total_debt"] == concept


def test_the_tesla_snapshot_reports_current_debt_and_nothing_settles_it():
    """The two sheet lines that would settle it are tsla: extension tags, not in the file."""
    from finsight.data import companyfacts_snapshot

    facts = companyfacts_snapshot("TSLA")
    k = KPIExtractor().extract_from_companyfacts(facts, "TSLA")
    assert (k.total_debt, k.provenance["concepts"]["total_debt"]) == (6_584, "LongTermDebt")
    assert "DebtCurrent ($1,569M) is reported for 2025-12-31" in k.provenance["limitations"]["current_debt"]
    gaap = facts["facts"]["us-gaap"]
    assert not {"LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligationsCurrent"} & set(gaap)


# --- while nothing settles Tesla's current debt, every surface says so ----------

FILING = "https://www.sec.gov/Archives/edgar/data/1318605/000162828026003952/tsla-20251231.htm"


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from finsight.api.app import app

    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    return TestClient(app)


def _says_it(note):
    assert "The demo's debt excludes current debt." in note
    assert "$1,569M of current debt beside $6,584M long-term at 2025-12-31" in note
    assert FILING in note


def test_dcf_says_it_and_what_it_does_to_the_value(client):
    body = client.get("/dcf/TSLA").json()
    note = body["limitations"]["current_debt"]
    _says_it(note)
    value = body["intrinsic_value_per_share"]
    per_share = 1_569 / body["assumptions"]["shares_outstanding_m"]
    assert f"${per_share:,.2f} a share" in note and "$0.44 a share" in note
    assert f"about {per_share / value:.0%} of the ${value:,.2f} value" in note
    assert "about 3% of the $13.19 value" in note
    assert body["provenance"]["limitations"]["current_debt"] == note


def test_the_stated_percent_is_the_arithmetic_of_the_engine(client):
    """Take $1,569M more net debt through the engine's own equity-value step."""
    from finsight.api import demo_data
    from finsight.models.dcf import DCFModel

    kpis = demo_data._kpis_for("TSLA")
    base = DCFModel().value_from_kpis(kpis).intrinsic_value_per_share
    kpis.total_debt += 1_569
    corrected = DCFModel().value_from_kpis(kpis).intrinsic_value_per_share
    assert base - corrected == pytest.approx(1_569 / kpis.shares_outstanding)
    assert (base - corrected) / base == pytest.approx(0.03, abs=0.005)


def test_another_year_is_sized_by_its_own_current_debt(client):
    note = client.get("/dcf/TSLA?period=2024-12-31").json()["limitations"]["current_debt"]
    _says_it(note)
    assert "at $2,343M, by about" in note
    assert "That $2,343M is the DebtCurrent reported for 2024-12-31." in note


def test_a_share_under_one_percent_is_not_rounded_to_nothing(client):
    note = client.get("/dcf/TSLA?period=2022").json()["limitations"]["current_debt"]
    assert "about 0.3% of the $95.49 value" in note


def test_the_latest_year_needs_no_such_line(client):
    assert "is the DebtCurrent reported for" not in client.get("/dcf/TSLA").json()["limitations"]["current_debt"]


def test_another_wacc_restates_the_percent(client):
    body = client.get("/dcf/TSLA?wacc=0.12").json()
    value = body["intrinsic_value_per_share"]
    assert f"of the ${value:,.2f} value" in body["limitations"]["current_debt"]


def test_kpis_says_it_without_a_value(client):
    note = client.get("/kpis/TSLA").json()["provenance"]["limitations"]["current_debt"]
    _says_it(note)
    assert "/dcf says by how much" in note


def test_the_report_says_it_beside_the_value_and_in_its_sources(client):
    text = client.get("/report/TSLA").text
    head = text[:text.index("## Summary")]
    assert "> **Current debt.** The demo's debt excludes current debt." in head
    assert "about 3% of the $13.19 value" in head and FILING in head
    assert "- DCF input limitation — current_debt: The demo's debt excludes" in text


def test_the_recording_says_it():
    import json
    from pathlib import Path

    recording = json.loads((Path(__file__).parents[1] / "frontend/lib/recordedDemo.json").read_text())
    _says_it(recording["tickers"]["TSLA"]["dcf"]["limitations"]["current_debt"])
    _says_it(recording["tickers"]["TSLA"]["kpis"]["provenance"]["limitations"]["current_debt"])


def test_the_readme_says_it():
    from pathlib import Path

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert "the demo's debt excludes current debt" in readme
    assert "$1,569M of current debt beside $6,584M long-term at 2025-12-31" in readme
    assert FILING in readme
    assert "about $0.44 a share, about 3% of the $13.19" in readme


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL"])
def test_no_other_filer_wears_it(client, ticker):
    assert "current_debt" not in client.get(f"/dcf/{ticker}").json()["limitations"]
    assert "Current debt." not in client.get(f"/report/{ticker}").text


def test_the_figures_quoted_are_the_ones_the_snapshot_can_check():
    from finsight.api.demo_data import KNOWN_CURRENT_DEBT
    from finsight.data import companyfacts_snapshot

    gap = KNOWN_CURRENT_DEBT["TSLA"]
    gaap = companyfacts_snapshot("TSLA")["facts"]["us-gaap"]
    for concept, figure in (("LongTermDebt", "long_term_m"), ("DebtCurrent", "current_m")):
        rows = gaap[concept]["units"]["USD"]
        assert {r["val"] for r in rows if r["end"] == gap["as_of"]} == {gap[figure] * 1_000_000}
    assert "000162828026003952" in gap["url"]
    assert {r["accn"] for r in gaap["DebtCurrent"]["units"]["USD"] if r["end"] == gap["as_of"]} == {
        "0001628280-26-003952"}


def test_evidence_that_settles_it_ends_the_disclosure():
    """The note lives only while the extractor says nothing settles the current debt."""
    from finsight.api import demo_data

    kpis = _extract({"LongTermDebt": 5_535, "DebtCurrent": 2_343})
    assert demo_data._known_debt_gap("TSLA", kpis) is not None
    kpis = _extract(TSLA_FY2024)
    assert demo_data._known_debt_gap("TSLA", kpis) is None
    kpis = _extract({"LongTermDebt": 5_535}, snapshot=SNAPSHOT)
    assert demo_data._known_debt_gap("TSLA", kpis) is None


def test_a_value_at_or_below_zero_gets_no_percent():
    from types import SimpleNamespace
    from finsight.api import demo_data

    kpis = _extract({"LongTermDebt": 5_535}, snapshot=SNAPSHOT)
    result = SimpleNamespace(intrinsic_value_per_share=-1.0,
                             assumptions=SimpleNamespace(shares_outstanding=1_000.0))
    note = demo_data._current_debt_disclosure(demo_data.KNOWN_CURRENT_DEBT["TSLA"], kpis, result)
    assert "/dcf says by how much" in note and "%" not in note


# --- no debt figure at all: flagged wherever an equity value is shown -----------

def _real_debt_elsewhere(debt_on_other_tag=None):
    """A filer whose debt is tagged outside the chain: $1,000M revenue-scale figures,
    the same company twice, so the difference in value is the debt."""
    from finsight.extraction.kpi_extractor import ExtractedKPIs

    k = ExtractedKPIs(ticker="X", period="FY2025", source_type="10-K", revenue=100_000.0,
                      revenue_growth_yoy=0.05, operating_income=25_000.0, ebitda=30_000.0,
                      capex=5_000.0, shares_outstanding=100.0, cash_and_equivalents=1_000.0)
    k.total_debt = debt_on_other_tag
    k.provenance = {"concepts": {}, "limitations": {}, "fiscal_year_end": "2025-12-31",
                    "missing": ["total_debt"] if debt_on_other_tag is None else []}
    return k


def test_a_missing_debt_figure_is_an_assumed_fundamental():
    """With no debt figure the value reads $200 a share high here, so net debt must be in assumed_fundamentals."""
    from finsight.models.dcf import DCFModel

    silent = DCFModel().value_from_kpis(_real_debt_elsewhere())
    told = DCFModel().value_from_kpis(_real_debt_elsewhere(20_000.0))
    assert silent.intrinsic_value_per_share - told.intrinsic_value_per_share == pytest.approx(200.0)
    assert "net_debt" in silent.assumptions.assumed_fundamentals
    assert silent.assumptions.sources["net_debt"].startswith("default: debt not reported, counted as 0")
    assert "debt_not_reported" in silent.assumptions.limitations
    assert "overstated by all of it" in silent.assumptions.limitations["debt_not_reported"]
    assert "net_debt" not in told.assumptions.assumed_fundamentals
    assert "debt_not_reported" not in told.assumptions.limitations


def test_a_missing_debt_figure_reaches_the_dcf_response_and_the_report():
    from finsight.api import payloads
    from finsight.models.dcf import DCFModel
    from finsight.report.generator import ReportGenerator

    kpis = _real_debt_elsewhere()
    result = DCFModel().value_from_kpis(kpis)
    body = payloads.dcf_payload("X", kpis, result, 5, mode="live")
    assert "net_debt" in body["assumed_fundamentals"] and "net_debt" in body["defaulted_inputs"]
    assert "debt_not_reported" in body["limitations"]
    gen = ReportGenerator()
    text = gen.to_markdown(gen.generate("X", kpis=kpis, dcf_result=result,
                                        data_sources=payloads.dcf_source_lines(result.assumptions)))
    head = text[:text.index("## Summary")]
    assert "the model assumed: net_debt" in head
    assert "DCF input limitation — debt_not_reported" in text


def test_a_filer_with_both_sides_reported_is_not_flagged():
    from finsight.models.dcf import DCFModel

    a = DCFModel().value_from_kpis(_real_debt_elsewhere(5_000.0)).assumptions
    assert a.assumed_fundamentals == [] or "net_debt" not in a.assumed_fundamentals
    assert a.sources["net_debt"].startswith("reported total debt")


def test_an_unreported_cash_figure_is_flagged_the_same_way():
    from finsight.models.dcf import DCFModel

    k = _real_debt_elsewhere(5_000.0)
    k.cash_and_equivalents = None
    a = DCFModel().value_from_kpis(k).assumptions
    assert "net_debt" in a.assumed_fundamentals
    assert "debt_not_reported" not in a.limitations


def test_the_dashboard_names_an_assumed_figure_beside_the_value():
    from pathlib import Path

    page = (Path(__file__).parents[1] / "frontend/app/page.tsx").read_text(encoding="utf-8")
    hero = page[page.index("DCF model value {missing"):page.index("Model vs price</div>")]
    assert "data.dcfAssumed.join" in hero
    assert "assumed by the model" in hero

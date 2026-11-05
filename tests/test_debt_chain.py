"""
What total debt is made of, for a filer that does not tag LongTermDebt.

The chain holds the common long-term tags, a total concept wins over its components, nothing
is counted twice, and what it cannot settle is said. Without it, $20B of SeniorNotes beside $1B
of CommercialPaper would read as $1B of total debt, and $33.83 a share as $233.83.
"""

import pytest
from fastapi.testclient import TestClient

from finsight.api import payloads
from finsight.extraction.kpi_extractor import KPIExtractor
from finsight.models.dcf import DCFModel

END = "2025-12-31"


def _row(value_m, start=None, **extra):
    row = {"end": END, "val": value_m * 1_000_000, "accn": "0000000001-26-000001", "fy": 2025,
           "fp": "FY", "form": "10-K", "filed": "2026-02-01", **extra}
    return {**row, "start": start} if start else row


def _facts(debt_tags):
    """A company worth $243.83 a share with no debt, on 100M shares."""
    year = "2025-01-01"
    gaap = {
        "Revenues": {"units": {"USD": [_row(10_000, year)]}},
        "OperatingIncomeLoss": {"units": {"USD": [_row(2_000, year)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [_row(400, year)]}},
        "DepreciationDepletionAndAmortization": {"units": {"USD": [_row(500, year)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_row(1_000)]}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [_row(100, year)]}},
    }
    gaap.update({c: {"units": {"USD": [_row(v)]}} for c, v in debt_tags.items()})
    return {"facts": {"us-gaap": gaap}}


def _kpis(debt_tags):
    return KPIExtractor().extract_from_companyfacts(_facts(debt_tags), "X")


def _read(debt_tags):
    k = _kpis(debt_tags)
    return k.total_debt, k.provenance["concepts"].get("total_debt"), k.provenance["limitations"]


# --- the two shapes that read $200 a share high ----------------------------------

def test_senior_notes_beside_commercial_paper_are_the_whole_debt():
    k = _kpis({"SeniorNotes": 20_000, "CommercialPaper": 1_000})
    assert k.total_debt == 21_000
    assert k.provenance["concepts"]["total_debt"] == "SeniorNotes + CommercialPaper (derived)"
    assert set(k.provenance["figure_filings"]) >= {"SeniorNotes", "CommercialPaper"}
    assert DCFModel().value_from_kpis(k).intrinsic_value_per_share == pytest.approx(33.83, abs=0.01)


def test_the_balance_sheet_pair_is_the_whole_debt_and_says_it_holds_leases():
    total, concept, limits = _read({"LongTermDebtAndCapitalLeaseObligations": 20_000,
                                    "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000})
    assert total == 21_000
    assert concept == ("LongTermDebtAndCapitalLeaseObligations + "
                       "LongTermDebtAndCapitalLeaseObligationsCurrent (derived)")
    assert "noncurrent finance leases are counted" in limits["long_term_debt_leases"]
    assert "current finance leases are counted" in limits["current_debt"]
    assert "except where a note on debt here says otherwise" in limits["finance_leases"]
    k = _kpis({"LongTermDebtAndCapitalLeaseObligations": 20_000,
               "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000})
    assert DCFModel().value_from_kpis(k).intrinsic_value_per_share == pytest.approx(33.83, abs=0.01)


def test_tagged_finance_leases_come_out_of_the_balance_sheet_lines():
    total, concept, limits = _read({
        "LongTermDebtAndCapitalLeaseObligations": 20_000,
        "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000,
        "FinanceLeaseLiabilityNoncurrent": 300, "FinanceLeaseLiabilityCurrent": 100})
    assert total == 20_600
    assert concept == ("(LongTermDebtAndCapitalLeaseObligations − FinanceLeaseLiabilityNoncurrent) + "
                       "(LongTermDebtAndCapitalLeaseObligationsCurrent − FinanceLeaseLiabilityCurrent)"
                       " (derived)")
    assert "long_term_debt_leases" not in limits and "current_debt" not in limits


# --- a total concept wins over its components ------------------------------------

def test_the_balance_sheet_total_holds_its_two_lines():
    total, concept, limits = _read({
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 21_000,
        "LongTermDebtAndCapitalLeaseObligations": 20_000,
        "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000})
    assert total == 21_000
    assert concept == "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"
    assert "long_term_debt_leases" in limits


def test_the_balance_sheet_total_takes_out_both_tagged_lease_figures():
    total, concept, limits = _read({
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 21_000,
        "FinanceLeaseLiabilityNoncurrent": 300, "FinanceLeaseLiabilityCurrent": 100})
    assert total == 20_600
    assert concept.startswith("(LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities − ")
    assert "long_term_debt_leases" not in limits


def test_notes_payable_holds_the_senior_and_convertible_notes():
    total, concept, _ = _read({"NotesPayable": 20_000, "SeniorNotes": 15_000,
                               "ConvertibleNotesPayable": 5_000, "NotesPayableCurrent": 1_000})
    assert (total, concept) == (20_000, "NotesPayable")


def test_a_class_total_holds_its_own_noncurrent_and_current_forms():
    total, concept, limits = _read({"SeniorNotes": 20_000, "SeniorNotesNoncurrent": 19_000,
                                    "SeniorNotesCurrent": 1_000})
    assert (total, concept) == (20_000, "SeniorNotes")
    assert "current_debt" not in limits


def test_long_term_debt_wins_over_every_wider_tag():
    total, concept, limits = _read({"LongTermDebt": 21_000, "SeniorNotes": 20_000,
                                    "ConvertibleNotesPayable": 1_000, "NotesPayable": 21_000,
                                    "LongTermDebtCurrent": 1_000, "LongTermDebtNoncurrent": 20_000})
    assert (total, concept) == (21_000, "LongTermDebt")
    assert "debt_above_total" not in limits and "long_term_debt_not_found" not in limits


def test_long_term_debt_noncurrent_wins_too():
    total, concept, _ = _read({"LongTermDebtNoncurrent": 20_000, "LongTermDebtCurrent": 1_000,
                               "SeniorNotes": 20_000, "SeniorNotesCurrent": 1_000})
    assert total == 21_000
    assert concept == "LongTermDebtNoncurrent + LongTermDebtCurrent (derived)"


# --- forms that add up -----------------------------------------------------------

@pytest.mark.parametrize("tags, concept", [
    ({"SeniorNotesNoncurrent": 20_000, "SeniorNotesCurrent": 1_000},
     "SeniorNotesNoncurrent + SeniorNotesCurrent (derived)"),
    ({"LongTermNotesPayable": 20_000, "NotesPayableCurrent": 1_000},
     "LongTermNotesPayable + NotesPayableCurrent (derived)"),
    ({"NotesPayableNoncurrent": 20_000, "NotesPayableCurrent": 1_000},
     "NotesPayableNoncurrent + NotesPayableCurrent (derived)"),
    ({"ConvertibleLongTermNotesPayable": 20_000, "ConvertibleNotesPayableCurrent": 1_000},
     "ConvertibleLongTermNotesPayable + ConvertibleNotesPayableCurrent (derived)"),
    ({"ConvertibleNotesPayableNoncurrent": 20_000, "ConvertibleNotesPayableCurrent": 1_000},
     "ConvertibleNotesPayableNoncurrent + ConvertibleNotesPayableCurrent (derived)"),
    ({"SeniorNotes": 20_000, "ConvertibleNotesPayable": 1_000},
     "SeniorNotes + ConvertibleNotesPayable (derived)"),
    ({"SeniorLongTermNotes": 20_000, "ConvertibleNotesPayable": 1_000},
     "SeniorLongTermNotes + ConvertibleNotesPayable (derived)"),
    ({"SeniorNotes": 20_000, "ConvertibleNotesPayableCurrent": 1_000},
     "SeniorNotes + ConvertibleNotesPayableCurrent (derived)"),
    ({"LongTermDebtAndCapitalLeaseObligations": 20_000, "LongTermDebtCurrent": 1_000},
     "LongTermDebtAndCapitalLeaseObligations + LongTermDebtCurrent (derived)"),
])
def test_a_noncurrent_form_adds_its_current_form_and_two_classes_add_up(tags, concept):
    total, read, _ = _read(tags)
    assert (total, read) == (21_000, concept)


def test_the_broader_current_concept_holds_a_class_current_form():
    total, concept, _ = _read({"SeniorNotesNoncurrent": 20_000, "SeniorNotesCurrent": 1_000,
                               "LongTermDebtCurrent": 1_000})
    assert total == 21_000
    assert concept == "SeniorNotesNoncurrent + LongTermDebtCurrent (derived)"


def test_debt_current_still_holds_the_short_term_borrowings_on_the_wider_path():
    total, concept, _ = _read({"SeniorNotesNoncurrent": 20_000, "DebtCurrent": 1_000,
                               "CommercialPaper": 500})
    assert total == 21_000
    assert concept == "SeniorNotesNoncurrent + DebtCurrent (derived)"


# --- what nothing settles is said, not guessed -----------------------------------

@pytest.mark.parametrize("current", ["LongTermDebtCurrent", "DebtCurrent", "NotesPayableCurrent"])
def test_current_debt_beside_a_total_concept_is_not_added_and_says_so(current):
    total, concept, limits = _read({"SeniorNotes": 20_000, current: 1_000})
    assert (total, concept) == (20_000, "SeniorNotes")
    assert f"{current} ($1,000M) is reported" in limits["current_debt"]
    assert "whether SeniorNotes already holds it" in limits["current_debt"]


def test_a_total_concept_with_no_current_figure_says_it_may_leave_it_out():
    total, _, limits = _read({"NotesPayable": 20_000})
    assert total == 20_000
    assert "nothing reconciles NotesPayable" in limits["current_debt"]
    assert "may leave current debt out" in limits["current_debt"]


def test_a_noncurrent_form_with_no_current_figure_says_it_may_leave_it_out():
    _, _, limits = _read({"LongTermNotesPayable": 20_000})
    assert "nothing reconciles LongTermNotesPayable" in limits["current_debt"]


@pytest.mark.parametrize("tags", [
    {"LongTermDebt": 1_000, "SeniorNotes": 20_000},
    {"LongTermDebtNoncurrent": 1_000, "ConvertibleNotesPayable": 20_000},
    {"LongTermDebt": 1_000, "LongTermDebtAndCapitalLeaseObligations": 20_000},
    {"NotesPayable": 1_000, "SeniorNotes": 20_000},
    {"LongTermNotesPayable": 1_000, "SeniorNotes": 20_000},
])
def test_a_figure_above_the_total_is_not_added_and_says_so(tags):
    total, _, limits = _read(tags)
    assert total == 1_000
    assert "$20,000M) is reported" in limits["debt_above_total"]
    assert "cannot be holding all of it" in limits["debt_above_total"]
    assert "$19,000M" in limits["debt_above_total"]


def test_a_line_read_net_of_large_tagged_leases_is_not_above_its_own_total():
    total, _, limits = _read({"LongTermDebtAndCapitalLeaseObligations": 20_000,
                              "FinanceLeaseLiabilityNoncurrent": 15_000, "LongTermDebtCurrent": 1_000})
    assert total == 6_000 and "debt_above_total" not in limits


def test_a_sheet_line_beside_long_term_debt_is_compared_without_its_tagged_leases():
    _, _, limits = _read({"LongTermDebt": 5_000, "LongTermDebtAndCapitalLeaseObligations": 5_500,
                          "FinanceLeaseLiabilityNoncurrent": 1_000, "DebtCurrent": 500})
    assert "debt_above_total" not in limits


def test_a_total_s_own_current_line_carries_no_note():
    for tags in ({"NotesPayable": 20_000, "NotesPayableCurrent": 1_000},
                 {"LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities": 20_000,
                  "LongTermDebtAndCapitalLeaseObligationsCurrent": 1_000}):
        total, _, limits = _read(tags)
        assert total == 20_000 and "current_debt" not in limits


def test_the_two_parts_above_long_term_debt_say_so():
    total, concept, limits = _read({"LongTermDebt": 20_400, "LongTermDebtNoncurrent": 20_000,
                                    "LongTermDebtCurrent": 1_000})
    assert (total, concept) == (20_400, "LongTermDebt")
    assert "LongTermDebtNoncurrent + LongTermDebtCurrent ($21,000M)" in limits["debt_above_total"]


def test_a_figure_within_rounding_of_the_total_is_not_flagged():
    _, _, limits = _read({"LongTermDebt": 20_000, "SeniorNotes": 20_100})
    assert "debt_above_total" not in limits


# --- short-term debt and no long-term concept --------------------------------------

@pytest.mark.parametrize("tag", ["CommercialPaper", "ShortTermBorrowings", "LongTermDebtCurrent",
                                 "DebtCurrent", "NotesPayableCurrent", "SeniorNotesCurrent",
                                 "ConvertibleNotesPayableCurrent",
                                 "LongTermDebtAndCapitalLeaseObligationsCurrent"])
def test_short_term_debt_alone_says_long_term_debt_was_not_found(tag):
    total, concept, limits = _read({tag: 1_000})
    assert (total, concept) == (1_000, tag)
    note = limits["long_term_debt_not_found"]
    assert note.startswith(f"Long-term debt was not found: {tag} is reported for {END}")
    assert "common tags, not every tag" in note
    assert "overstated by all of it" in note


def test_the_note_is_not_blamed_on_the_filer_when_a_snapshot_never_fetched_the_tags():
    series = {c: [{"end": END, "val": 1e9, "unit": "USD", "fy": 2025, "form": "10-K",
                   "first_fy": 2025, "accn": "a", "first_accn": "a", "filed": "2026-02-01"}]
              for c in ("Revenues", "CommercialPaper")}
    k = KPIExtractor().extract_from_xbrl(
        series, "X", snapshot={"retrieved": "2026-10-31", "not_carried": ["SeniorNotes"]})
    assert "is in this bundled snapshot, which does not carry them all" in \
        k.provenance["limitations"]["long_term_debt_not_found"]


@pytest.mark.parametrize("tags", [
    {},
    {"LongTermDebt": 5_000, "CommercialPaper": 1_000},
    {"LongTermDebtNoncurrent": 5_000, "ShortTermBorrowings": 1_000},
    {"SeniorNotes": 5_000, "CommercialPaper": 1_000},
    {"LongTermDebtAndCapitalLeaseObligations": 5_000, "CommercialPaper": 1_000},
])
def test_long_term_debt_found_or_no_debt_at_all_carries_no_such_note(tags):
    _, _, limits = _read(tags)
    assert "long_term_debt_not_found" not in limits


def test_no_debt_at_all_is_still_debt_not_reported_and_says_the_list_is_common_tags():
    assumptions = DCFModel().value_from_kpis(_kpis({})).assumptions
    note = assumptions.limitations["debt_not_reported"]
    assert "SeniorNotes" in note and "common tags, not every tag" in note
    assert "overstated by all of it" in note


# --- every /dcf response names the concepts ----------------------------------------

@pytest.mark.parametrize("tags, concepts", [
    ({"SeniorNotes": 20_000, "CommercialPaper": 1_000}, ["SeniorNotes", "CommercialPaper"]),
    ({"LongTermDebt": 5_000}, ["LongTermDebt"]),
    ({"LongTermDebtAndCapitalLeaseObligations": 20_000, "FinanceLeaseLiabilityNoncurrent": 300,
      "CommercialPaper": 100},
     ["LongTermDebtAndCapitalLeaseObligations", "FinanceLeaseLiabilityNoncurrent", "CommercialPaper"]),
    ({}, []),
])
def test_the_live_dcf_response_names_what_total_debt_is_made_of(monkeypatch, tags, concepts):
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    import finsight.api.app as app_module

    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000000001")
    monkeypatch.setattr(app_module._edgar, "get_company_facts", lambda cik: _facts(tags))
    body = TestClient(app_module.app).get("/dcf/ZZZZ").json()
    assert body["total_debt_concepts"] == concepts
    assert ("debt_not_reported" in body["limitations"]) == (not tags)


@pytest.mark.parametrize("ticker, concepts", [
    ("AAPL", ["LongTermDebt", "CommercialPaper"]),
    ("MSFT", ["LongTermDebt"]),
    ("NVDA", ["LongTermDebt"]),
    ("GOOGL", ["LongTermDebt", "CommercialPaper"]),
    ("TSLA", ["LongTermDebt"]),
])
def test_the_demo_dcf_response_names_them_and_no_new_warning_fires(monkeypatch, ticker, concepts):
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    from finsight.api.app import app

    body = TestClient(app).get(f"/dcf/{ticker}").json()
    assert body["total_debt_concepts"] == concepts
    assert not {"long_term_debt_not_found", "debt_above_total", "long_term_debt_leases"} \
        & set(body["limitations"])


def test_a_report_says_it_beside_the_value():
    from finsight.report.generator import ReportGenerator

    for tags, title in (({"CommercialPaper": 1_000}, "> **Long-term debt.** Long-term debt was not found"),
                        ({"LongTermDebt": 1_000, "SeniorNotes": 20_000}, "> **Total debt.** SeniorNotes")):
        kpis = _kpis(tags)
        result = DCFModel().value_from_kpis(kpis)
        gen = ReportGenerator()
        text = gen.to_markdown(gen.generate("X", kpis=kpis, dcf_result=result,
                                            data_sources=payloads.dcf_source_lines(result.assumptions)))
        assert title in text[:text.index("## Summary")]


def test_the_dashboard_lists_them_first():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "frontend/lib/research.ts").read_text(encoding="utf-8")
    line = next(s for s in source.splitlines() if "VALUE_LIMITATIONS = " in s)
    assert "long_term_debt_not_found" in line and "debt_above_total" in line


def test_the_readme_says_the_chain_is_a_list_of_common_tags():
    from pathlib import Path

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert "The chain is a list of common tags, not every tag" in readme
    assert "a filer using other tags can read low" in readme
    assert "`total_debt_concepts`" in readme
    assert "`limitations.long_term_debt_not_found`" in readme

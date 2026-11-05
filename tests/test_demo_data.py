"""
Demo data must say what it is and be what it says.

Demo mode runs the live extraction path over real SEC companyfacts snapshots (retrieved
2026-11-04), so each label comes from the filing it describes. These tests pin the figures to
the cited 10-Ks and check that every derived figure is consistent with the reported ones
beside it.
"""

import copy
import importlib
from pathlib import Path

import pytest

from finsight.data import SNAPSHOT_RETRIEVED, companyfacts_snapshot

# (fiscal year, year-end, 10-K accession, revenue $M, prior-year revenue $M,
#  diluted shares M) — from each company's 10-K via SEC companyfacts.
CITED = {
    "AAPL": ("FY2026", "2026-09-26", "0000320193-26-000026", 478_834, 416_161, 14_720.306),
    "MSFT": ("FY2026", "2026-06-30", "0001193125-26-323660", 331_839, 281_724, 7_453),
    "NVDA": ("FY2026", "2026-01-25", "0001045810-26-000021", 215_938, 130_497, 24_514),
    "GOOGL": ("FY2025", "2025-12-31", "0001652044-26-000018", 402_836, 350_018, 12_230),
    "TSLA": ("FY2025", "2025-12-31", "0001628280-26-003952", 94_827, 97_690, 3_528),
}


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    import finsight.api.demo_data as demo_data

    importlib.reload(demo_data)
    return demo_data


@pytest.mark.parametrize("ticker", sorted(CITED))
def test_label_and_figures_come_from_the_cited_10k(demo, ticker):
    fy, end, accession, revenue, prior, shares = CITED[ticker]
    k = demo.demo_kpis(ticker)
    assert k["mode"] == "demo"
    assert k["period"] == fy == k["provenance"]["fiscal_year"]
    assert k["provenance"]["fiscal_year_end"] == end
    assert k["provenance"]["accession"] == accession
    assert k["provenance"]["source"] == (
        f"bundled SEC EDGAR companyfacts snapshot (retrieved {SNAPSHOT_RETRIEVED})")
    assert k["revenue"] == pytest.approx(revenue)
    assert k["shares_outstanding"] == pytest.approx(shares)
    # Growth is this year's revenue against the prior year's — the same pair.
    assert k["revenue_growth_yoy"] == pytest.approx(revenue / prior - 1)


@pytest.mark.parametrize("ticker", sorted(CITED))
def test_derived_figures_agree_with_the_reported_ones(demo, ticker):
    k = demo.demo_kpis(ticker)
    assert k["provenance"]["missing"] == []
    assert k["gross_margin"] == pytest.approx(k["gross_profit"] / k["revenue"])
    assert k["operating_margin"] == pytest.approx(k["operating_income"] / k["revenue"])
    assert k["net_margin"] == pytest.approx(k["net_income"] / k["revenue"])
    # Diluted EPS × diluted shares ≈ net income (rounding of reported EPS and
    # share counts aside; 3%).
    assert k["eps_diluted"] * k["shares_outstanding"] == pytest.approx(k["net_income"], rel=0.03)


@pytest.mark.parametrize("ticker", sorted(CITED))
def test_every_figure_is_in_the_bundled_sec_snapshot(demo, ticker):
    """No figure is typed in by hand: each reported field's concept has that
    exact value at the fiscal year-end in the bundled SEC payload."""
    k = demo.demo_kpis(ticker)
    gaap = companyfacts_snapshot(ticker)["facts"]["us-gaap"]
    end = k["provenance"]["fiscal_year_end"]
    for field in ("revenue", "operating_income", "net_income", "capex",
                  "cash_and_equivalents", "eps_diluted", "shares_outstanding"):
        concept = k["provenance"]["concepts"][field]
        (unit,) = gaap[concept]["units"]
        values = {e["val"] for e in gaap[concept]["units"][unit] if e["end"] == end}
        scale = 1 if unit == "USD/shares" else 1e6
        assert any(abs(v / scale - k[field]) < 1e-9 for v in values), (field, concept)


def test_prices_are_dated(demo):
    for ticker in demo.DEMO_TICKERS:
        meta = demo.demo_meta(ticker)
        assert meta["price_date"] == "2026-10-30"
        assert meta["price_source"] == "Yahoo Finance daily close"


def test_demo_honours_a_requested_fiscal_year(demo):
    k = demo.demo_kpis("MSFT", period="2024")
    assert (k["period"], k["revenue"]) == ("FY2024", pytest.approx(245_122))
    with pytest.raises(ValueError, match="fiscal year 2015"):
        demo.demo_kpis("MSFT", period="2015")


def test_demo_dcf_matches_the_independent_derivation(demo):
    from tests.hand_derivation import APPLE_FY2026, hand_dcf

    body = demo.demo_dcf("AAPL")
    assert body["mode"] == "demo" and body["fiscal_year"] == "FY2026"
    assert body["intrinsic_value_per_share"] == pytest.approx(hand_dcf(**APPLE_FY2026), rel=1e-12)


# ---------------------------------------------------------------------------
# Every figure is cited to the filing it came from
# ---------------------------------------------------------------------------

def _filings_reporting(ticker, concept, unit, end):
    import json
    from pathlib import Path
    facts = json.loads((Path(__file__).parents[1] / "finsight/data/companyfacts"
                        / f"{ticker}.json").read_text())
    rows = facts["facts"]["us-gaap"][concept]["units"][unit]
    return {e["accn"] for e in rows if e.get("end") == end and e.get("form") == "10-K"}


def test_an_older_year_cites_its_own_10k_and_each_figure_its_source():
    """/kpis/AAPL?period=2022 cited the FY2024 10-K (0000320193-24-000123)
    for a 2022-09-24 cash balance that only the FY2022 and FY2023 10-Ks
    report."""
    from finsight.api import demo_data

    p = demo_data.demo_kpis("AAPL", "2022")["provenance"]
    assert (p["accession"], p["filed"]) == ("0000320193-22-000108", "2022-10-28")
    cash = p["figure_filings"]["CashAndCashEquivalentsAtCarryingValue"]["accession"]
    assert cash == "0000320193-23-000106"
    assert cash in _filings_reporting("AAPL", "CashAndCashEquivalentsAtCarryingValue",
                                      "USD", "2022-09-24")
    for concept, filing in p["figure_filings"].items():
        unit = "shares" if "Shares" in concept else ("USD/shares" if "PerShare" in concept else "USD")
        assert filing["accession"] in _filings_reporting("AAPL", concept, unit, "2022-09-24"), concept


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_the_latest_year_still_cites_the_same_10k(ticker):
    """Guard: for the default (latest) year the cited 10-K is unchanged."""
    from finsight.api import demo_data

    p = demo_data.demo_kpis(ticker)["provenance"]
    assert {f["accession"] for f in p["figure_filings"].values()} == {p["accession"]}


def test_share_counts_say_they_are_not_split_adjusted():
    """NVDA's FY2022 count (2,535M) predates its 10:1 split and FY2023's (25,070M) follows it, and the response says so."""
    from finsight.api import demo_data

    fy22 = demo_data.demo_kpis("NVDA", "2022")
    fy23 = demo_data.demo_kpis("NVDA", "2023")
    assert fy22["shares_outstanding"] == 2535.0 and fy23["shares_outstanding"] == 25070.0
    assert fy22["provenance"]["per_share_basis"] == (
        "as reported in the 10-K filed 2024-02-21 (0001045810-24-000029); not adjusted "
        "for any stock split after that filing")
    assert "filed 2025-02-26" in fy23["provenance"]["per_share_basis"]
    dcf = demo_data.demo_dcf("NVDA")
    assert dcf["assumption_sources"]["shares_outstanding"].endswith(
        "not adjusted for any stock split after that filing")


# ---------------------------------------------------------------------------
# The snapshots say exactly what they carry
# ---------------------------------------------------------------------------

def _extractor_concepts():
    from finsight.extraction.kpi_extractor import KPIExtractor
    return {c for chain in KPIExtractor.XBRL_CONCEPTS.values() for c, _ in chain}


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_every_concept_the_extractor_reads_is_carried_or_a_declared_gap(ticker):
    """A concept read but not carried must be listed as a known gap (and in the file's
    not_carried), or this fails. The files say what they were trimmed to, so that stays true."""
    assert _snapshot_problems(ticker) == []


def _snapshot_problems(ticker):
    from finsight.data import KNOWN_SNAPSHOT_GAPS, companyfacts_snapshot

    snap = companyfacts_snapshot(ticker)
    meta = snap["_snapshot"]
    declared = set(meta["concepts"])
    problems = []
    if not set(snap["facts"]["us-gaap"]) <= declared:
        problems.append("a bundled concept is not declared")
    gaps = _extractor_concepts() - declared
    if not gaps <= set(KNOWN_SNAPSHOT_GAPS):
        problems.append(f"undeclared snapshot gap: {sorted(gaps - set(KNOWN_SNAPSHOT_GAPS))}")
    if sorted(gaps) != meta["not_carried"]:
        problems.append(f"not_carried {meta['not_carried']} != {sorted(gaps)}")
    if f"the {len(declared)} us-gaap concepts" not in meta["contents"]:
        problems.append("contents does not state the concept count")
    return problems


def test_the_gap_check_catches_a_newly_read_concept(monkeypatch):
    """The guard itself: reading one more concept without declaring it fails."""
    from finsight.extraction.kpi_extractor import KPIExtractor
    monkeypatch.setitem(KPIExtractor.XBRL_CONCEPTS, "goodwill", (("Goodwill", "USD"),))
    assert any("undeclared snapshot gap: ['Goodwill']" in p for p in _snapshot_problems("AAPL"))


def _without_amortization(ticker):
    """A file cut before FinSight read AmortizationOfIntangibleAssets."""
    import copy

    from finsight.data import companyfacts_snapshot

    facts = copy.deepcopy(companyfacts_snapshot(ticker))
    facts["facts"]["us-gaap"].pop("AmortizationOfIntangibleAssets", None)
    facts["_snapshot"].update(not_carried=["AmortizationOfIntangibleAssets"], retrieved="2026-10-31")
    return facts


def test_a_snapshot_gap_is_not_reported_as_the_filers():
    """A concept the file never fetched is unknown, not the filer's omission."""
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFModel

    msft = KPIExtractor().extract_from_companyfacts(_without_amortization("MSFT"), "MSFT")
    assert ("AmortizationOfIntangibleAssets is not in the bundled snapshot (cut 2026-10-31, "
            "before FinSight read it), so whether the filer reports it is unknown"
            ) in msft.provenance["limitations"]["depreciation_amortization"]
    aapl = KPIExtractor().extract_from_companyfacts(_without_amortization("AAPL"), "AAPL")
    note = DCFModel().value_from_kpis(aapl).assumptions.limitations["steady_state_capex"]
    assert "the bundled snapshot does not carry AmortizationOfIntangibleAssets" in note


def test_every_bundled_file_carries_what_the_extractor_reads():
    from finsight.data import KNOWN_SNAPSHOT_GAPS, companyfacts_snapshot

    assert KNOWN_SNAPSHOT_GAPS == {}
    for ticker in ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"):
        assert companyfacts_snapshot(ticker)["_snapshot"]["not_carried"] == []


# ---------------------------------------------------------------------------
# The snapshot's earliest year: its trim, not the filer's gap
# ---------------------------------------------------------------------------

EARLIEST_YEAR = {"AAPL": ("2021", "2020-09-27"), "TSLA": ("2021", "2020-12-31"),
                 "GOOGL": ("2021", "2020-12-31"), "MSFT": ("2022", "2021-06-30")}


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from finsight.api.app import app
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    return TestClient(app)


@pytest.mark.parametrize("ticker", sorted(EARLIEST_YEAR))
def test_the_earliest_year_blames_the_snapshot_not_sec(client, ticker):
    """The earliest year's missing growth is the bundled file's cut at FY2021, not SEC's: the response must not say
    "no prior-year revenue"."""
    year, cut = EARLIEST_YEAR[ticker]
    source = client.get(f"/dcf/{ticker}?period={year}").json()["assumption_sources"]["revenue_growth_rates"]
    assert f"keeps only periods ending after {cut}" in source
    assert "cut out of the file" in source and "no prior-year revenue" not in source
    kpis = client.get(f"/kpis/{ticker}?period={year}").json()
    assert kpis["revenue_growth_yoy"] is None
    assert f"periods ending after {cut}" in kpis["provenance"]["limitations"]["revenue_growth_yoy"]


def test_the_later_years_do_not_carry_that_note(client):
    body = client.get("/dcf/AAPL?period=2022").json()
    assert "revenue_growth_yoy" not in body["provenance"]["limitations"]
    assert "snapshot" not in body["assumption_sources"]["revenue_growth_rates"]


def test_the_note_moves_no_figure(client):
    """The note is words only: FY2021 AAPL's value is the engine's."""
    body = client.get("/dcf/AAPL?period=2021").json()
    assert body["intrinsic_value_per_share"] == pytest.approx(69.61, abs=0.005)


# ---------------------------------------------------------------------------
# The snapshots: pinned, and held to what they say they are
# ---------------------------------------------------------------------------

def test_the_snapshots_match_their_manifest():
    """Any change to a file fails here: no other test reads, say, Apple's FY2022 R&D."""
    from finsight.data import snapshot_checksum_problems

    assert snapshot_checksum_problems() == []


def _snapshot_copy(tmp_path):
    import shutil
    from finsight.data import _DIR

    target = tmp_path / "companyfacts"
    shutil.copytree(_DIR, target)
    return target


def test_a_tampered_figure_fails_the_manifest(tmp_path):
    import json
    from finsight.data import snapshot_checksum_problems

    directory = _snapshot_copy(tmp_path)
    assert snapshot_checksum_problems(directory) == []
    path = directory / "AAPL.json"
    facts = json.loads(path.read_text())
    rows = facts["facts"]["us-gaap"]["ResearchAndDevelopmentExpense"]["units"]["USD"]
    fy2022 = [r for r in rows if r["end"] == "2022-09-24"]
    assert fy2022
    fy2022[0]["val"] += 1_000_000_000
    path.write_text(json.dumps(facts))
    assert snapshot_checksum_problems(directory) == ["AAPL.json: sha-256 does not match SHA256SUMS"]


def test_an_added_or_removed_snapshot_fails_the_manifest(tmp_path):
    from finsight.data import snapshot_checksum_problems

    directory = _snapshot_copy(tmp_path)
    (directory / "TSLA.json").unlink()
    (directory / "AMZN.json").write_text("{}")
    assert snapshot_checksum_problems(directory) == [
        "AMZN.json: not in SHA256SUMS", "TSLA.json: in SHA256SUMS, not on disk"]


@pytest.mark.parametrize("ticker", sorted(CITED))
def test_each_snapshot_is_trimmed_as_it_states(ticker):
    """The rules the files state are tested on the files themselves."""
    import re

    snap = companyfacts_snapshot(ticker)
    meta = snap["_snapshot"]
    after = re.search(r"periods ending after (\d{4}-\d{2}-\d{2})", meta["contents"]).group(1)
    assert list(snap["facts"]) == ["us-gaap"]
    for concept, body in snap["facts"]["us-gaap"].items():
        assert concept in meta["concepts"], concept
        for rows in body["units"].values():
            for row in rows:
                assert row["form"] == "10-K", (concept, row)
                assert row["end"] > after, (concept, row)


@pytest.mark.parametrize("ticker", sorted(CITED))
def test_a_snapshot_does_not_call_a_trimmed_concept_unreported(ticker):
    """A listed concept absent from facts may just be outside the window: AAPL's and MSFT's `Revenues` are absent here,
    and both filers reported it in earlier years."""
    contents = companyfacts_snapshot(ticker)["_snapshot"]["contents"]
    assert "is not in the filer's 10-K facts" not in contents
    assert "has no 10-K fact for a period in this window" in contents
    assert "the filer may have reported it for earlier periods" in contents


def test_the_docs_say_revenues_is_outside_the_window():
    from pathlib import Path

    root = Path(__file__).parents[1]
    for path in (root / "README.md", root / "docs/usage.md"):
        assert "bundled snapshot's window" in path.read_text(encoding="utf-8"), path.name
    for ticker in ("AAPL", "MSFT"):
        assert "Revenues" not in companyfacts_snapshot(ticker)["facts"]["us-gaap"]
        assert "Revenues" in companyfacts_snapshot(ticker)["_snapshot"]["concepts"]


def test_the_readme_says_how_the_files_were_cut():
    from pathlib import Path

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    row = next(l for l in readme.splitlines() if l.startswith("| **Bundled SEC snapshots**"))
    assert "periods ending after a per-file date" in row
    assert "SHA256SUMS" in row
    assert "scripts/refresh_snapshots.py" in row


# ---------------------------------------------------------------------------
# A year outside the snapshot: the snapshot's limit, not the filer's
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["kpis", "dcf"])
@pytest.mark.parametrize("period", ["2020", "FY2020"])
def test_a_year_outside_the_snapshot_blames_the_trim(client, endpoint, period):
    """A year outside the file is the file's limit: the detail must not say Apple reported no revenue for it."""
    response = client.get(f"/{endpoint}/AAPL?period={period}")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "No annual 10-K revenue reported" not in detail
    assert "No fiscal year 2020 in AAPL's bundled demo snapshot" in detail
    assert "keeps only 10-K periods ending after 2020-09-27" in detail
    assert "latest available: FY2023, FY2024, FY2025, FY2026" in detail
    assert "unset FINSIGHT_DEMO" in detail


def test_an_exact_year_end_outside_the_snapshot_blames_the_trim_too(client):
    detail = client.get("/kpis/AAPL?period=2020-09-26").json()["detail"]
    assert "No fiscal year 2020-09-26 in AAPL's bundled demo snapshot" in detail


def test_a_year_in_the_snapshot_is_still_served(client):
    assert client.get("/kpis/AAPL?period=2022").json()["period"] == "FY2022"


# Reported FCF, the projected formula on that year's own figures, DCF Year 1 ($M),
# from the bundled snapshots, 2026-11-04; the README's table states them.
TWO_FCFS = {
    "AAPL": (136_056, 129_252, 147_275),
    "MSFT": (66_987, 45_689, 52_636),
    "NVDA": (96_676, 96_910, 133_946),
    "GOOGL": (73_266, 31_630, 35_187),
    "TSLA": (6_220, -57, 0.7),
}


@pytest.mark.parametrize("ticker", TWO_FCFS)
def test_the_dcf_says_how_its_fcf_differs_from_the_reported_one(demo, ticker):
    """Tesla's reported FCF is $6.2B and its DCF Year 1 $0.7M: the response reconciles the two."""
    reported, base_year, year_1 = TWO_FCFS[ticker]
    dcf = demo.demo_dcf(ticker)
    assert demo.demo_kpis(ticker)["free_cash_flow"] == pytest.approx(reported, abs=0.5)
    assert dcf["projected_fcfs_m"][0] == pytest.approx(year_1, abs=0.5)
    note = dcf["limitations"]["free_cash_flow_basis"]
    assert f"operating cash flow − CapEx, ${reported:,.0f}M for {dcf['fiscal_year']}" in note
    money = f"−${-base_year:,.0f}M" if base_year < 0 else f"${base_year:,.0f}M"
    assert f"before growth and ΔNWC, gives {money}" in note
    assert "stock-based compensation" in demo.demo_report(ticker)


def test_the_snapshots_carry_no_stock_comp_figure_to_split_the_two_fcfs():
    """What the limitation and the README say: the gap cannot be split from this data."""
    from finsight.extraction.kpi_extractor import KPIExtractor

    for ticker in TWO_FCFS:
        gaap = companyfacts_snapshot(ticker)["facts"]["us-gaap"]
        assert not [c for c in gaap if "ShareBased" in c or "StockBased" in c]
    read = {c for chain in KPIExtractor.XBRL_CONCEPTS.values() for c, _ in chain}
    assert not [c for c in read if "ShareBased" in c or "StockBased" in c]


def test_the_docs_state_the_two_fcfs_and_what_the_dcf_deducts():
    root = Path(__file__).parents[1]
    text = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    arch = " ".join((root / "docs/architecture.md").read_text(encoding="utf-8").split())
    for doc in (text, arch):
        assert "stock-based compensation" in doc
        assert "operating cash flow − CapEx" in doc
    for ticker, (reported, base_year, year_1) in TWO_FCFS.items():
        money = f"−${-base_year:,.0f}M" if base_year < 0 else f"${base_year:,.0f}M"
        last = "$0.7M" if year_1 < 1 else f"${year_1:,.0f}M"
        assert f"| {ticker} | ${reported:,.0f}M | {money} | {last} |" in text


def test_tesla_states_both_share_counts_and_what_the_choice_moves(demo):
    """3,528M "diluted" is below the 3,751M at year-end: on that count $13.19 is $12.41, more than the $0.44 of current
    debt, and a limitation says so."""
    from tests.hand_derivation import dollars_at_year_end_shares

    dcf = demo.demo_dcf("TSLA")
    assert round(dcf["intrinsic_value_per_share"], 2) == 13.19      # no value change
    assert dcf["assumptions"]["shares_outstanding_m"] == 3_528
    note = dcf["limitations"]["share_count"]
    assert "3,528M diluted weighted-average" in note and "3,751M shares outstanding" in note
    other = dollars_at_year_end_shares(dcf["intrinsic_value_per_share"], 3_528, 3_751)
    assert f"${other:,.2f}" == "$12.41" and "value would be $12.41" in note
    assert "$0.78, or 5.9%, lower" in note
    assert "not which count is right" in note
    assert "share_count" in demo.demo_report("TSLA")


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL"])
def test_a_year_end_count_below_the_diluted_one_needs_no_note(demo, ticker):
    assert "share_count" not in demo.demo_dcf(ticker)["limitations"]


def test_the_readme_states_both_of_teslas_share_counts():
    text = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert "On 3,751M, the $13.19 value would be $12.41: $0.78, or 5.9%, lower" in text


def test_a_count_from_another_filing_is_not_compared(demo):
    """Two filings can straddle a split: NVDA's FY2023 diluted count is post-split, its
    year-end count pre-split. Here the year-end count is 10x, from a later 10-K."""
    from finsight.extraction.kpi_extractor import KPIExtractor

    facts = copy.deepcopy(companyfacts_snapshot("TSLA"))
    rows = facts["facts"]["us-gaap"]["CommonStockSharesOutstanding"]["units"]["shares"]
    held = [r for r in rows if r["end"] == "2025-12-31"]
    assert held and all(r["val"] == 3_751_000_000 for r in held)
    for row in held:
        row.update(val=35_280_000_000, accn="0000000000-26-000001", filed="2026-09-30")
    kpis = KPIExtractor().extract_from_companyfacts(facts, "TSLA")
    assert kpis.shares_outstanding == 3_528
    assert kpis.provenance["period_end_shares_above_diluted"] is None


# --- two years, two filings, either side of a split: said, values unchanged --------

def test_two_years_either_side_of_nvidia_s_split_say_so(client):
    """/dcf/NVDA reads $100.26 for 2022 and $1.83 for 2023: the counts sit across its split."""
    fy22 = client.get("/dcf/NVDA", params={"period": "2022"}).json()
    fy23 = client.get("/dcf/NVDA", params={"period": "2023"}).json()
    assert round(fy22["intrinsic_value_per_share"], 2) == 100.26
    assert round(fy23["intrinsic_value_per_share"], 2) == 1.83
    for body, here, other in ((fy22, "2,535M here (10-K filed 2024-02-21)", "25,070M"),
                              (fy23, "25,070M here (10-K filed 2025-02-26)", "2,535M")):
        note = body["limitations"]["stock_split_basis"]
        assert here in note and f"against {other} for the fiscal year ended" in note
        assert "what a stock split looks like" in note and "not comparable" in note
    kpis = client.get("/kpis/NVDA", params={"period": "2022"}).json()
    assert "stock_split_basis" in kpis["provenance"]["limitations"]


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_no_latest_year_wears_the_split_note(client, ticker):
    assert "stock_split_basis" not in client.get(f"/dcf/{ticker}").json()["limitations"]


def test_only_nvidia_s_two_years_wear_the_split_note():
    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor

    worn = []
    for ticker in ("AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"):
        facts = companyfacts_snapshot(ticker)
        rows = facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]
        for end in sorted({r["end"] for r in rows["units"]["shares"]}):
            try:
                k = KPIExtractor().extract_from_companyfacts(facts, ticker, end)
            except ValueError:
                continue
            if "stock_split_basis" in k.provenance["limitations"]:
                worn.append((ticker, end))
    assert worn == [("NVDA", "2022-01-30"), ("NVDA", "2023-01-29")]


def test_a_jump_inside_one_filing_is_real_and_not_flagged():
    from finsight.extraction.kpi_extractor import _share_basis_note

    row = {"accn": "a", "filed": "2026-01-01"}
    rows = [{**row, "end": "2024-12-31", "val": 100e6}, {**row, "end": "2025-12-31", "val": 300e6}]
    assert _share_basis_note(rows, "2025-12-31") is None
    rows[0] = {**rows[0], "accn": "b", "filed": "2025-01-01"}
    assert "100M for the fiscal year ended 2024-12-31" in _share_basis_note(rows, "2025-12-31")
    assert _share_basis_note([{**rows[0], "val": 280e6}, rows[1]], "2025-12-31") is None

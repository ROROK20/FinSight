"""
scripts/refresh_snapshots.py re-cuts the bundled files from live SEC companyfacts. A run
of it needs the maintainer's identity, so this exercises its cut offline on stubbed
payloads, and the bundled files are its own output. No test here reaches SEC.
"""

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("refresh_snapshots", ROOT / "scripts/refresh_snapshots.py")
refresh_snapshots = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh_snapshots)

BUNDLED = ROOT / "finsight/data/companyfacts"
TICKERS = ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]


def _bundled(ticker):
    return json.loads((BUNDLED / f"{ticker}.json").read_text(encoding="utf-8"))


def _row(end, val, form="10-K", **more):
    return {"accn": "0001628280-25-003063", "end": end, "filed": "2025-01-30", "form": form,
            "fp": "FY", "fy": 2024, "val": val, **more}


def _payload(ticker):
    """What SEC would answer for this filer: the bundled facts, as a payload."""
    snap = _bundled(ticker)
    return {"cik": snap["cik"], "entityName": snap["entityName"], "facts": copy.deepcopy(snap["facts"])}


def _tesla_with_current_debt():
    """A payload with Tesla's FY2024 figures under the us-gaap sheet tags (its real sheet
    lines are tsla: extension tags), with what the cut must leave out."""
    payload = _payload("TSLA")
    payload["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"label": "x", "units": {"shares": []}}}
    gaap = payload["facts"]["us-gaap"]
    for concept, val in (("DebtCurrent", 2_343), ("LongTermDebtAndCapitalLeaseObligationsCurrent", 2_456),
                         ("LongTermDebtAndCapitalLeaseObligations", 5_757),
                         ("FinanceLeaseLiabilityCurrent", 113), ("FinanceLeaseLiabilityNoncurrent", 222)):
        gaap[concept] = {"label": concept, "description": "SEC's long text, not bundled",
                         "units": {"USD": [_row("2024-12-31", val * 1_000_000)]}}
    gaap["DebtCurrent"]["units"]["USD"] += [
        _row("2024-09-30", 1, form="10-Q"),      # not a 10-K
        _row("2019-12-31", 2),                   # before the file's window
        _row("2020-12-31", 3),                   # on the window's date: "after" excludes it
        _row("2024-12-31", 4, form="10-K/A"),    # the bundled rule is 10-K alone
    ]
    gaap["Goodwill"] = {"label": "Goodwill", "units": {"USD": [_row("2024-12-31", 5)]}}
    return payload


def _fetch(overrides=None):
    by_cik = {f"{_bundled(t)['cik']:010d}": (overrides or {}).get(t) or _payload(t) for t in TICKERS}
    calls = []

    def fetch(cik):
        calls.append(cik)
        return by_cik[cik]

    fetch.calls = calls
    return fetch


# --- the cut --------------------------------------------------------------------

def test_the_cut_keeps_the_10k_facts_it_reads_and_leaves_out_the_rest():
    new = refresh_snapshots.cut(_tesla_with_current_debt(), "2020-12-31", "2026-11-01")
    gaap = new["facts"]["us-gaap"]
    assert list(new["facts"]) == ["us-gaap"]
    assert "Goodwill" not in gaap
    assert [r["val"] for r in gaap["DebtCurrent"]["units"]["USD"]] == [2_343_000_000]
    assert gaap["DebtCurrent"] == {"label": "DebtCurrent", "units": gaap["DebtCurrent"]["units"]}


def test_the_cut_changes_no_value():
    payload = _tesla_with_current_debt()
    new = refresh_snapshots.cut(payload, "2020-12-31", "2026-11-01")
    for concept, node in new["facts"]["us-gaap"].items():
        for unit, rows in node["units"].items():
            for row in rows:
                assert row in payload["facts"]["us-gaap"][concept]["units"][unit]


@pytest.mark.parametrize("ticker", TICKERS)
def test_the_cut_of_a_bundled_payload_is_that_file(ticker):
    """The stub is the file's own facts, so this shows the rules lose and change none of
    them. It says nothing about a live payload or the original cut."""
    snap = _bundled(ticker)
    new = refresh_snapshots.cut(_payload(ticker), refresh_snapshots.window_of(snap), "2026-10-31")
    assert new["facts"] == snap["facts"]
    assert (new["cik"], new["entityName"]) == (snap["cik"], snap["entityName"])
    assert new["_snapshot"]["source_url"] == snap["_snapshot"]["source_url"]


def test_the_cut_states_what_the_tests_hold_a_snapshot_to():
    meta = refresh_snapshots.cut(_tesla_with_current_debt(), "2020-12-31", "2026-11-01")["_snapshot"]
    assert meta["concepts"] == refresh_snapshots.concepts_read()
    assert meta["not_carried"] == []
    assert meta["retrieved"] == "2026-11-01"
    assert f"the {len(meta['concepts'])} us-gaap concepts" in meta["contents"]
    assert "periods ending after 2020-12-31" in meta["contents"]
    assert "has no 10-K fact for a period in this window" in meta["contents"]
    assert "the filer may have reported it for earlier periods" in meta["contents"]
    assert "is not in the filer's 10-K facts" not in meta["contents"]


@pytest.mark.parametrize("ticker", TICKERS)
def test_it_writes_the_files_byte_for_byte_as_they_are_written(ticker):
    """So a refresh diffs by fact, not by format."""
    raw = (BUNDLED / f"{ticker}.json").read_text(encoding="utf-8")
    assert refresh_snapshots.dumps(json.loads(raw)) == raw


def test_it_writes_the_manifest_as_it_is_written():
    assert refresh_snapshots.checksums(BUNDLED) == (BUNDLED / "SHA256SUMS").read_text()


# --- a refresh, end to end, offline ---------------------------------------------

def test_a_refresh_writes_the_set_pins_it_and_says_what_moved(tmp_path):
    from finsight.data import snapshot_checksum_problems

    fetch = _fetch({"TSLA": _tesla_with_current_debt()})
    lines = refresh_snapshots.refresh(fetch, BUNDLED, tmp_path, "2026-11-01")
    assert sorted(fetch.calls) == sorted(f"{_bundled(t)['cik']:010d}" for t in TICKERS)
    assert snapshot_checksum_problems(tmp_path) == []
    assert "TSLA LongTermDebtAndCapitalLeaseObligations: 1 added, 0 gone, 0 changed" in lines
    assert "AAPL: no fact moved" in lines
    assert any(line.startswith("AAPL FY2026: total debt $80,111M") for line in lines)
    assert not any(line.startswith("AAPL FY2026 now:") for line in lines)


def test_sheet_lines_under_tags_the_extractor_reads_settle_it_and_the_disclosure_stops(tmp_path):
    from finsight.api import demo_data
    from finsight.extraction.kpi_extractor import KPIExtractor

    refresh_snapshots.refresh(_fetch({"TSLA": _tesla_with_current_debt()}), BUNDLED, tmp_path, "2026-11-01")
    snap = json.loads((tmp_path / "TSLA.json").read_text(encoding="utf-8"))
    kpis = KPIExtractor().extract_from_companyfacts(snap, "TSLA", "2024-12-31")
    assert kpis.total_debt == 7_878
    assert kpis.provenance["concepts"]["total_debt"] == "LongTermDebt + DebtCurrent (derived)"
    assert "current_debt" not in kpis.provenance["limitations"]
    assert demo_data._known_debt_gap("TSLA", kpis) is None


def test_a_moved_fact_is_named_with_both_values(tmp_path):
    payload = _payload("TSLA")
    rows = payload["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"]
    for row in rows:
        if row["end"] == "2025-12-31":
            row["val"] += 1_000_000
    lines = refresh_snapshots.refresh(_fetch({"TSLA": payload}), BUNDLED, tmp_path, "2026-11-01")
    assert "TSLA LongTermDebt at 2025-12-31: 6,584,000,000 -> 6,585,000,000" in lines
    assert any(line.startswith("TSLA FY2025 now: total debt $6,585M") for line in lines)


def test_one_failed_fetch_writes_nothing(tmp_path):
    def fetch(cik):
        if cik == f"{_bundled('NVDA')['cik']:010d}":
            raise ConnectionError("SEC did not answer")
        return _fetch()(cik)

    with pytest.raises(ConnectionError):
        refresh_snapshots.refresh(fetch, BUNDLED, tmp_path, "2026-11-01")
    assert list(tmp_path.iterdir()) == []


def test_another_filers_payload_is_refused(tmp_path):
    with pytest.raises(ValueError, match="asked for CIK 320193"):
        refresh_snapshots.refresh(lambda cik: _payload("TSLA"), BUNDLED, tmp_path, "2026-11-01")
    assert list(tmp_path.iterdir()) == []


def test_a_payload_with_nothing_to_keep_is_refused(tmp_path):
    empty = lambda cik: {"cik": int(cik), "facts": {"us-gaap": {}}}  # noqa: E731
    with pytest.raises(ValueError, match="nothing left after the cut"):
        refresh_snapshots.refresh(empty, BUNDLED, tmp_path, "2026-11-01")
    assert list(tmp_path.iterdir()) == []


# --- the identity ---------------------------------------------------------------

@pytest.fixture()
def no_sec(monkeypatch):
    from finsight.ingestion.edgar import EdgarClient

    def _blocked(self, cik):
        raise AssertionError("a request was made")

    monkeypatch.setattr(EdgarClient, "get_company_facts", _blocked)


@pytest.mark.parametrize("identity", [None, "", "Your Name your-email@example.com", "FinSight no contact",
                                      "Your Name you@yourdomain"])
def test_it_refuses_to_run_without_an_identity(monkeypatch, no_sec, capsys, tmp_path, identity):
    if identity is None:
        monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    else:
        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", identity)
    assert refresh_snapshots.main(["--directory", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "Refusing to run" in err and "SEC_EDGAR_USER_AGENT" in err
    assert list(tmp_path.iterdir()) == []


def test_with_an_identity_it_reports_and_lists_what_to_re_derive(monkeypatch, capsys, tmp_path):
    from finsight.ingestion.edgar import EdgarClient

    fetch = _fetch()
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "FinSight Test Suite finsight-tests@university.edu")
    monkeypatch.setattr(EdgarClient, "get_company_facts", lambda self, cik: fetch(cik))
    assert refresh_snapshots.main(["--directory", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "TSLA: no fact moved" in out
    for step in ("SNAPSHOT_RETRIEVED", "python scripts/record_demo.py", "documented values",
                 "KNOWN_CURRENT_DEBT", "node e2e/capture.mjs"):
        assert step in out
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [f"{t}.json" for t in TICKERS] + ["SHA256SUMS"])


# --- the docs -------------------------------------------------------------------

def test_the_readme_gives_the_command_and_what_follows_it():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r'^SEC_EDGAR_USER_AGENT="Your Name you@yourdomain" python scripts/refresh_snapshots\.py$',
                     readme, re.M)
    section = readme[readme.index("### Refreshing the bundled snapshots"):]
    section = section[:section.index("\n---")]
    for step in ("SNAPSHOT_RETRIEVED", "scripts/record_demo.py", "documented values",
                 "`mode` live", "exercises the cut offline"):
        assert step in section, step
    assert "has not yet been run" not in readme


def test_the_module_docs_stop_saying_the_cut_cannot_be_rerun():
    from finsight import data

    assert "scripts/refresh_snapshots.py" in data.__doc__
    assert "so the cut\ncannot be re-run from here" not in data.__doc__

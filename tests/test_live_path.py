"""
The LIVE /kpis, /dcf and /report path, driven by real SEC companyfacts.

What is guarded: the share count is read from ``units.shares``, revenue from the
concept the filer actually reports (Apple's is the ASC 606 one), the period is the
latest in the data, no gap is filled with a placeholder, and the response carries
its ``mode``.

These tests run the live endpoints offline against the bundled snapshot of
Apple's real companyfacts (retrieved 2026-11-04), with the stale ``Revenues``
facts Apple's real payload still carries (through FY2018) put back in, and
check the result against a valuation derived independently below.
"""

import copy
import importlib
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finsight.data import companyfacts_snapshot
from tests.hand_derivation import APPLE_FY2026, hand_dcf

# A real fact from Apple's full companyfacts payload (10-K filed 2018-11-05):
# `Revenues` stops in FY2018, so reading it would report stale revenue for the current year.
_STALE_REVENUES = {
    "start": "2017-10-01", "end": "2018-09-29", "val": 265_595_000_000,
    "accn": "0000320193-18-000145", "fy": 2018, "fp": "FY", "form": "10-K",
    "filed": "2018-11-05", "frame": "CY2018",
}


def apple_facts(drop=()):
    facts = copy.deepcopy(companyfacts_snapshot("AAPL"))
    gaap = facts["facts"]["us-gaap"]
    gaap["Revenues"] = {"label": "Revenues", "units": {"USD": [_STALE_REVENUES]}}
    for concept in drop:
        gaap.pop(concept, None)
    return facts


@pytest.fixture()
def live(monkeypatch):
    """The app on its LIVE path, SEC calls answered from a stub."""
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    assert not demo_data.demo_enabled()

    state = {"facts": apple_facts()}
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000320193")
    monkeypatch.setattr(app_module._edgar, "get_company_facts", lambda cik: state["facts"])
    app_module.state = state
    return app_module


def test_live_kpis_report_the_latest_fiscal_year_in_the_right_units(live):
    body = TestClient(live.app).get("/kpis/AAPL").json()
    assert body["mode"] == "live"
    assert body["period"] == "FY2026"
    assert body["revenue"] == pytest.approx(478_834.0)          # not FY2018's 265,595
    assert body["shares_outstanding"] == pytest.approx(14_720.306)  # units.shares
    assert body["provenance"]["fiscal_year_end"] == "2026-09-26"
    assert body["provenance"]["accession"] == "0000320193-26-000026"


def test_live_dcf_values_the_real_company_and_says_what_it_used(live):
    body = TestClient(live.app).get("/dcf/AAPL").json()
    assert body["mode"] == "live"
    assert body["fiscal_year"] == "FY2026"
    assert body["provenance"]["fiscal_year_end"] == "2026-09-26"
    a = body["assumptions"]
    assert a["base_revenue_m"] == pytest.approx(478_834.0)
    assert a["shares_outstanding_m"] == pytest.approx(14_720.306)
    assert a["net_debt_m"] == pytest.approx(80_111 - 36_305)
    assert body["intrinsic_value_per_share"] == pytest.approx(hand_dcf(**APPLE_FY2026), rel=1e-12)
    # Only modelling parameters are defaults — every fundamental is reported.
    # WACC and terminal growth the caller did not pass ARE defaults: they used
    # to be labelled "caller" because the endpoint filled them in itself, so
    # the dashboard counted 2 defaults while using a generic 10% WACC.
    assert body["defaulted_inputs"] == [
        "nwc_pct_of_revenue_change", "tax_rate", "terminal_growth_rate", "wacc"]
    assert body["assumption_sources"]["wacc"] == "default: 10.0%"
    assert set(body["adjustments"]) == {"revenue_growth_rates", "capex_as_pct_revenue"}


def test_a_wacc_the_caller_passes_is_the_callers(live):
    body = TestClient(live.app).get("/dcf/AAPL?wacc=0.09&terminal_growth=0.025").json()
    assert body["assumption_sources"]["wacc"] == "caller"
    assert body["assumption_sources"]["terminal_growth_rate"] == "caller"
    assert body["defaulted_inputs"] == ["nwc_pct_of_revenue_change", "tax_rate"]
    assert body["intrinsic_value_per_share"] == pytest.approx(
        hand_dcf(**APPLE_FY2026, wacc=0.09, g=0.025), rel=1e-12)


def test_a_basic_share_count_fallback_is_labelled_as_basic(live):
    """With no diluted count the extractor uses the period-end basic count, and the
    source says so."""
    live.state["facts"] = apple_facts(drop=("WeightedAverageNumberOfDilutedSharesOutstanding",))
    body = TestClient(live.app).get("/dcf/AAPL").json()
    assert body["assumptions"]["shares_outstanding_m"] == pytest.approx(14_532.259)
    assert body["assumption_sources"]["shares_outstanding"].startswith(
        "reported period-end basic shares outstanding, FY2026")


def test_live_dcf_refuses_without_a_share_count(live):
    live.state["facts"] = apple_facts(drop=(
        "WeightedAverageNumberOfDilutedSharesOutstanding", "CommonStockSharesOutstanding"))
    resp = TestClient(live.app).get("/dcf/AAPL")
    assert resp.status_code == 422
    assert "shares_outstanding not reported for FY2026" in resp.json()["detail"]


@pytest.mark.parametrize("query", [
    "wacc=0.05&terminal_growth=0.05",    # was ZeroDivisionError -> 500
    "wacc=0.05&terminal_growth=0.049",   # was $5,896.74/share
    "wacc=0.06&terminal_growth=0.045",
])
@pytest.mark.parametrize("demo", [False, True])
def test_dcf_rejects_a_wacc_too_close_to_terminal_growth(live, monkeypatch, query, demo):
    if demo:
        monkeypatch.setenv("FINSIGHT_DEMO", "1")
    resp = TestClient(live.app, raise_server_exceptions=False).get(f"/dcf/AAPL?{query}")
    assert resp.status_code == 422
    assert "must exceed terminal_growth" in resp.json()["detail"]


def test_live_request_without_a_user_agent_is_a_503_not_a_borrowed_identity(monkeypatch):
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)

    def _network(*a, **k):
        raise AssertionError("a request was sent without the caller's User-Agent")

    monkeypatch.setattr(app_module._edgar._session, "get", _network)
    client = TestClient(app_module.app, raise_server_exceptions=False)
    for path in ("/kpis/AAPL", "/dcf/AAPL", "/filings/AAPL"):
        resp = client.get(path)
        assert resp.status_code == 503, (path, resp.status_code, resp.text[:200])
        assert "SEC_EDGAR_USER_AGENT" in resp.json()["detail"]


def test_live_request_under_an_invented_identity_is_a_503_and_sends_nothing(monkeypatch):
    """A contact that reaches nobody must not carry live traffic."""
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "FinSight demo demo@example.com")
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    sent = []
    monkeypatch.setattr(app_module._edgar._session, "get",
                        lambda url, **kw: sent.append((url, kw.get("headers"))))
    client = TestClient(app_module.app, raise_server_exceptions=False)
    for path in ("/kpis/AAPL", "/dcf/AAPL", "/filings/AAPL"):
        resp = client.get(path)
        assert resp.status_code == 503, (path, resp.status_code, resp.text[:200])
        assert "no deliverable contact email" in resp.json()["detail"]
    assert sent == [], f"requests went out under an invented identity: {sent}"


def test_a_requested_fiscal_year_is_honoured_or_refused(live):
    client = TestClient(live.app)
    fy23 = client.get("/kpis/AAPL?period=2023").json()
    assert (fy23["period"], fy23["revenue"]) == ("FY2023", pytest.approx(383_285.0))
    missing = client.get("/kpis/AAPL?period=2012")
    assert missing.status_code == 404
    assert "fiscal year 2012" in missing.json()["detail"]


def test_a_live_failure_served_from_the_fixture_says_why(live, monkeypatch):
    def _down(cik):
        raise RuntimeError("SEC unreachable")

    monkeypatch.setattr(live._edgar, "get_company_facts", _down)
    body = TestClient(live.app).get("/dcf/AAPL").json()
    assert body["mode"] == "demo"
    assert "RuntimeError" in body["fallback_reason"]


def test_live_report_cites_its_filing_and_fiscal_year(live, monkeypatch):
    class _Profile:
        name = "Apple Inc."

    monkeypatch.setattr(live._market, "get_company_profile", lambda t: _Profile())
    monkeypatch.setattr(live._market, "get_price_history",
                        lambda t, period="1d": pd.DataFrame({"Close": [344.63]}))
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    text = TestClient(live.app).get("/report/AAPL").text
    assert "| Revenue (FY2026) | $478.8B |" in text
    assert "FY2026 10-K, fiscal year ended 2026-09-26 (accession 0000320193-26-000026)" in text
    assert f"${hand_dcf(**APPLE_FY2026):.2f}" in text


class TestMainEntryPoint:
    def _run(self, monkeypatch):
        import uvicorn
        import finsight.api.app as app_module

        seen = {}
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        monkeypatch.setattr("dotenv.find_dotenv", lambda *a, **k: "")
        app_module.main()
        return seen

    def test_defaults_to_loopback_without_reload(self, monkeypatch):
        """main() binds loopback without reload by default."""
        for var in ("FINSIGHT_HOST", "FINSIGHT_RELOAD"):
            monkeypatch.delenv(var, raising=False)
        seen = self._run(monkeypatch)
        assert seen["host"] == "127.0.0.1"
        assert seen["reload"] is False

    def test_bind_and_reload_are_explicit_opt_ins(self, monkeypatch):
        monkeypatch.setenv("FINSIGHT_HOST", "0.0.0.0")
        monkeypatch.setenv("FINSIGHT_RELOAD", "1")
        seen = self._run(monkeypatch)
        assert (seen["host"], seen["reload"]) == ("0.0.0.0", True)


def test_live_report_dates_its_price_by_the_bar(live, monkeypatch):
    """The live report dates the price by its last price bar, beside the
    valuation date (the base fiscal year-end)."""
    class _Profile:
        name = "Apple Inc."

    monkeypatch.setattr(live._market, "get_company_profile", lambda t: _Profile())
    monkeypatch.setattr(
        live._market, "get_price_history",
        lambda t, period="1d": pd.DataFrame(
            {"Close": [344.63]}, index=pd.DatetimeIndex(["2026-11-02"])))
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    text = TestClient(live.app).get("/report/AAPL").text
    assert "**Valuation date:** 2026-09-26" in text
    assert "**Price date:** 2026-11-02 — 37 days apart" in text


# ---------------------------------------------------------------------------
# Yahoo Finance failing must not take the report down
# ---------------------------------------------------------------------------

class _YahooDown(Exception):
    pass


def _yahoo_down(*a, **k):
    raise _YahooDown("Too Many Requests. Rate limited.")


def test_report_survives_yahoo_failing(live, monkeypatch):
    """get_company_profile / get_price_history sat outside the per-component
    guards, so a Yahoo rate limit 500'd the report and discarded the SEC,
    KPI and DCF sections."""
    monkeypatch.setattr(live._market, "get_company_profile", _yahoo_down)
    monkeypatch.setattr(live._market, "get_price_history", _yahoo_down)
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    r = TestClient(live.app, raise_server_exceptions=False).get("/report/AAPL")
    assert r.status_code == 200
    assert "| Revenue (FY2026) | $478.8B |" in r.text
    assert f"${hand_dcf(**APPLE_FY2026):.2f}" in r.text
    assert "Price unavailable: Yahoo Finance did not answer (_YahooDown)" in r.text
    assert "Company name unavailable: Yahoo Finance did not answer (_YahooDown)" in r.text
    assert "| N/A | N/A |" in r.text   # no price, so no model-vs-price figure


def test_live_meta_fallback_says_why(live, monkeypatch):
    monkeypatch.setattr(live._market, "get_company_profile", _yahoo_down)
    body = TestClient(live.app).get("/meta/AAPL").json()
    assert body["mode"] == "demo"
    assert body["fallback_reason"] == (
        "The live Yahoo Finance lookup failed (_YahooDown); this is the bundled demo data instead.")


def test_live_sentiment_fallback_says_why(live, monkeypatch):
    monkeypatch.setenv("FINSIGHT_LIVE_TRANSCRIPTS", "1")

    def _broken(t, n):
        raise RuntimeError("scraper exploded")
    monkeypatch.setattr(live, "_transcript_sentiments", _broken)
    body = TestClient(live.app).get("/sentiment/AAPL").json()
    assert body["mode"] == "demo"
    assert body["fallback_reason"].startswith("The live transcript lookup failed (RuntimeError)")


# ---------------------------------------------------------------------------
# A stock split between the share count and the price
# ---------------------------------------------------------------------------

def test_report_withholds_model_vs_price_across_a_split(live, monkeypatch):
    """The 10-K share count is as reported and Yahoo's price split-adjusted, so a 10:1 split after the filing would read
    as a ~-90% gap: it is withheld."""
    class _Profile:
        name = "Apple Inc."

    monkeypatch.setattr(live._market, "get_company_profile", lambda t: _Profile())
    monkeypatch.setattr(
        live._market, "get_price_history",
        lambda t, period="1d": pd.DataFrame({"Close": [34.46]}, index=pd.DatetimeIndex(["2026-11-10"])))
    monkeypatch.setattr(live._market, "get_splits", lambda t: [{"date": "2026-11-04", "ratio": 10.0}])
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    text = TestClient(live.app).get("/report/AAPL").text
    assert "**Model vs price withheld:** stock split (10:1 on 2026-11-04) after the share count's " \
           "10-K (filed 2026-11-03)" in text
    assert "| $165.31 | $34.46 | N/A |" in text


def test_a_split_before_the_filing_leaves_the_gap(live, monkeypatch):
    from finsight.report.generator import split_between
    assert split_between("2025-10-31", "2026-11-02", [{"date": "2020-08-31", "ratio": 4.0}]) is None
    assert split_between("2025-10-31", "2026-11-02", []) is None
    # None is an unknown history, not an empty one: it withholds.
    assert "could not be retrieved" in split_between("2025-10-31", "2026-11-02", None)


def test_live_meta_returns_the_split_history(live, monkeypatch):
    class _Profile:
        name, sector = "Apple Inc.", "Technology"
    monkeypatch.setattr(live._market, "get_company_profile", lambda t: _Profile())
    monkeypatch.setattr(live._market, "get_price_history",
                        lambda t, period="1d": pd.DataFrame({"Close": [250.0]}))
    monkeypatch.setattr(live._market, "get_splits", lambda t: [{"date": "2020-08-31", "ratio": 4.0}])
    assert TestClient(live.app).get("/meta/AAPL").json()["splits"] == [{"date": "2020-08-31", "ratio": 4.0}]


# ---------------------------------------------------------------------------
# SEC EDGAR failing is not "an unexpected error"
# ---------------------------------------------------------------------------

def _http_error(status):
    import requests
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=response)


def _timeout():
    import requests
    return requests.Timeout("SEC took too long")


def _connection_error():
    import requests
    return requests.ConnectionError("no route to host")


@pytest.fixture
def sec_fails(monkeypatch):
    """A live client whose SEC EDGAR calls raise what ``EdgarClient._get`` raises.
    ``where`` is "facts" (companyfacts and the filing index) or "cik" (the ticker map)."""
    import finsight.api.app as app_module
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Jane Doe jane@stub-not-real.io")

    def install(error, where="facts"):
        def boom(*_a, **_k):
            raise error
        if where == "cik":
            monkeypatch.setattr(app_module._edgar, "get_cik", boom)
        else:
            monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000019617")
            monkeypatch.setattr(app_module._edgar, "get_company_facts", boom)
            monkeypatch.setattr(app_module._edgar, "get_filings", boom)
        return TestClient(app_module.app, raise_server_exceptions=False)
    return install


@pytest.mark.parametrize("path", ["/kpis/JPM", "/dcf/JPM", "/filings/JPM"])
def test_sec_404_is_a_404_saying_sec_has_no_data(sec_fails, path):
    """A stubbed companyfacts 404 on a non-demo ticker is a 404, never a 500."""
    r = sec_fails(_http_error(404)).get(path)
    assert r.status_code == 404
    assert "SEC EDGAR holds no data for JPM" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/kpis/JPM", "/dcf/JPM", "/filings/JPM"])
@pytest.mark.parametrize("make,why", [(lambda: _http_error(403), "HTTP 403"),
                                      (lambda: _http_error(429), "HTTP 429"),
                                      (lambda: _http_error(500), "HTTP 500"),
                                      (lambda: _http_error(503), "HTTP 503"),
                                      (_timeout, "Timeout"),
                                      (_connection_error, "ConnectionError")])
def test_any_other_sec_failure_is_a_503(sec_fails, path, make, why):
    r = sec_fails(make()).get(path)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail == f"SEC EDGAR did not answer for JPM ({why}). Try again later."
    assert "Client Error" not in detail      # no raw exception text


@pytest.mark.parametrize("path", ["/meta/JPM", "/kpis/JPM", "/dcf/JPM", "/filings/JPM",
                                  "/sentiment/JPM", "/report/JPM"])
def test_the_ticker_map_failing_is_a_503_on_every_endpoint(sec_fails, path):
    r = sec_fails(_http_error(503), where="cik").get(path)
    assert r.status_code == 503, r.text
    assert "SEC EDGAR did not answer for JPM (HTTP 503)" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/kpis/AAPL?period=2021", "/dcf/AAPL?period=2021"])
def test_a_requested_year_is_never_answered_by_a_fixture(sec_fails, path):
    assert sec_fails(_http_error(404)).get(path).status_code == 404
    assert sec_fails(_http_error(503)).get(path).status_code == 503


@pytest.mark.parametrize("path", ["/meta/AAPL", "/sentiment/AAPL"])
def test_a_bundled_ticker_falls_back_when_the_ticker_map_fails(sec_fails, path):
    r = sec_fails(_http_error(503), where="cik").get(path)
    assert r.status_code == 200
    assert r.json()["mode"] == "demo" and "fallback_reason" in r.json()


@pytest.mark.parametrize("path", ["/kpis/AAPL", "/dcf/AAPL"])
def test_a_bundled_ticker_still_falls_back_to_its_fixture(sec_fails, path):
    r = sec_fails(_http_error(503)).get(path)
    assert r.status_code == 200
    assert r.json()["mode"] == "demo" and "fallback_reason" in r.json()


def test_the_live_report_withholds_the_gap_when_yahoo_has_no_split_history(live, monkeypatch):
    """The report and the dashboard agree: Yahoo's split call down is unknown, not no split."""
    def no_history(_ticker):
        raise RuntimeError("Yahoo is down")

    monkeypatch.setattr(live._market, "get_company_profile",
                        lambda t: SimpleNamespace(name="Apple Inc."))
    monkeypatch.setattr(
        live._market, "get_price_history",
        lambda t, period="1d": pd.DataFrame({"Close": [344.63]}, index=pd.DatetimeIndex(["2026-10-30"])))
    monkeypatch.setattr(live._market, "get_splits", no_history)
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    text = TestClient(live.app).get("/report/AAPL").text
    assert "**Model vs price withheld:** the split history could not be retrieved" in text
    assert "| $165.31 | $344.63 | N/A |" in text
    assert "Split history unavailable" in text


# ---------------------------------------------------------------------------
# A NaN close from Yahoo is no price
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("close", [float("nan"), float("inf")])
def test_a_non_finite_close_is_no_price_not_a_500(live, monkeypatch, close):
    """JSON cannot carry NaN, so a non-finite close is no price at all."""
    monkeypatch.setattr(live._market, "get_company_profile",
                        lambda t: SimpleNamespace(name="Apple Inc.", sector="Technology"))
    monkeypatch.setattr(
        live._market, "get_price_history",
        lambda t, period="1d": pd.DataFrame({"Close": [close]}, index=pd.DatetimeIndex(["2026-11-02"])))
    monkeypatch.setattr(live._market, "get_splits", lambda t: [])
    monkeypatch.setattr(live._market, "get_peers", lambda t: [])
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    client = TestClient(live.app, raise_server_exceptions=False)

    meta = client.get("/meta/AAPL")
    assert meta.status_code == 200
    assert meta.json()["current_price"] is None and meta.json()["mode"] == "live"

    report = client.get("/report/AAPL")
    assert report.status_code == 200
    assert "$nan" not in report.text and "$inf" not in report.text
    assert "Price unavailable: Yahoo Finance returned no usable close" in report.text
    assert "| $165.31 | N/A | N/A |" in report.text


def test_a_real_close_is_still_the_price(live, monkeypatch):
    monkeypatch.setattr(live._market, "get_company_profile",
                        lambda t: SimpleNamespace(name="Apple Inc.", sector="Technology"))
    monkeypatch.setattr(
        live._market, "get_price_history",
        lambda t, period="1d": pd.DataFrame({"Close": [250.5]}, index=pd.DatetimeIndex(["2026-11-02"])))
    monkeypatch.setattr(live._market, "get_splits", lambda t: [])
    assert TestClient(live.app).get("/meta/AAPL").json()["current_price"] == 250.5


# ---------------------------------------------------------------------------
# Who it covers: 10-K filers with the concepts it reads
# ---------------------------------------------------------------------------

def _row(val):
    return {"start": "2024-01-01", "end": "2024-12-31", "val": val, "fy": 2024, "fp": "FY",
            "form": "10-K", "filed": "2025-02-01", "accn": "0000000000-25-000001"}


def test_a_20f_filer_is_told_why_it_has_no_figures(live):
    """/dcf/TSM names the real reason: it is a valid US-listed company."""
    live.state["facts"] = {"cik": 1046179, "entityName": "A 20-F filer", "facts": {"ifrs-full": {
        "Revenue": {"units": {"TWD": [{**_row(1), "form": "20-F"}]}}}}}
    client = TestClient(live.app)
    r = client.get("/dcf/TSM")
    assert r.status_code == 404
    assert "10-K" in r.json()["detail"] and "20-F" in r.json()["detail"]
    assert "US-listed" not in r.json()["detail"]
    assert client.get("/kpis/TSM").status_code == 404


def test_a_bank_with_no_operating_income_gets_its_kpis_and_a_refused_dcf(live):
    live.state["facts"] = {"cik": 19617, "entityName": "A bank", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [_row(180_000_000_000)]}},
        "NetIncomeLoss": {"units": {"USD": [_row(50_000_000_000)]}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [_row(2_800_000_000)]}},
    }}}
    client = TestClient(live.app)
    assert client.get("/kpis/JPM").json()["revenue"] == 180_000
    r = client.get("/dcf/JPM")
    assert r.status_code == 422
    assert "neither operating income nor EBITDA" in r.json()["detail"]


def test_no_page_says_any_sec_registered_company():
    """What the banners and the demo's 404 say, against the two tests above."""
    from pathlib import Path

    from finsight.api import demo_data

    root = Path(__file__).parents[1]
    for rel in ("frontend/app/page.tsx", "finsight/api/demo_data.py", "frontend/README.md"):
        text = " ".join((root / rel).read_text(encoding="utf-8").split())
        assert "SEC-registered" not in text and "any ticker works" not in text, rel
    page = " ".join((root / "frontend/app/page.tsx").read_text(encoding="utf-8").split())
    assert page.count("{LIVE_SCOPE}") == 2 and "a foreign filer on Form 20-F has none" in page
    assert "10-K XBRL facts only" in demo_data.not_bundled_detail("AMZN")
    assert "that files 10-Ks" in demo_data.not_bundled_detail("AMZN")


# ---------------------------------------------------------------------------
# The error contract, as the README states it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "JPM"])
@pytest.mark.parametrize("endpoint", ["meta", "kpis", "dcf", "sentiment", "filings", "report"])
def test_no_user_agent_is_a_503_on_every_endpoint_bundled_or_not(monkeypatch, endpoint, ticker):
    """/sentiment/AAPL with no identity is the 503, not an answer from the synthetic fixture."""
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)

    def _network(*a, **k):
        raise AssertionError("a request was sent without the caller's User-Agent")

    monkeypatch.setattr(app_module._edgar._session, "get", _network)
    r = TestClient(app_module.app, raise_server_exceptions=False).get(f"/{endpoint}/{ticker}")
    assert r.status_code == 503, r.text[:200]
    assert "SEC_EDGAR_USER_AGENT" in r.json()["detail"]


def test_a_yahoo_outage_on_a_ticker_with_no_fixture_is_a_503(live, monkeypatch):
    """/meta/TSM with Yahoo down is a 503, not a 404 as if SEC had no such company."""
    monkeypatch.setattr(live._market, "get_company_profile", _yahoo_down)
    r = TestClient(live.app).get("/meta/TSM")
    assert r.status_code == 503
    assert r.json()["detail"] == "Yahoo Finance did not answer for TSM (_YahooDown). Try again later."


def test_the_readme_error_contract_names_both():
    from pathlib import Path

    text = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert "`SEC_EDGAR_USER_AGENT` unset or undeliverable → `503` with instructions, on every endpoint" in text
    assert "Yahoo Finance failing on `/meta` → `503`" in text


@pytest.mark.parametrize("path", ["/meta/AAPL", "/sentiment/AAPL"])
def test_a_fallback_names_the_lookup_that_failed_not_one_never_attempted(sec_fails, path):
    reason = sec_fails(_http_error(503), where="cik").get(path).json()["fallback_reason"]
    assert reason == ("The SEC EDGAR ticker lookup failed (HTTPError); "
                      "this is the bundled demo data instead.")


@pytest.mark.parametrize("path", ["/report/AAPL", "/filings/AAPL"])
def test_the_report_and_filings_have_no_fixture_when_the_ticker_map_fails(sec_fails, path):
    r = sec_fails(_http_error(503), where="cik").get(path)
    assert r.status_code == 503
    assert "SEC EDGAR did not answer for AAPL" in r.json()["detail"]


def test_the_error_contract_says_which_endpoints_fall_back():
    from pathlib import Path

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    assert ("On `/kpis`, `/dcf`, `/meta` and `/sentiment` a bundled ticker with no year "
            "requested falls back") in readme
    assert "`/filings` has no fixture, and `/report` none for a failed ticker-map lookup" in readme

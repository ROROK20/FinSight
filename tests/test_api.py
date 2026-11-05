"""
Tests for the FinSight FastAPI app.

Covers:
  * /health — basic liveness + demo-mode reporting.
  * the demo/offline path (FINSIGHT_DEMO=1) for every ticker endpoint — /meta,
    /filings, /kpis, /dcf, /sentiment, /report — asserting real-shaped,
    non-empty output produced by the actual engine, each response tagged so a
    consumer can tell demo output from live.
  * that demo mode makes NO network call: the network-touching clients are
    replaced with ones that raise, so a regression that reintroduces a live
    request fails the suite instead of quietly hitting SEC EDGAR / Yahoo.

These run fully offline: no SEC EDGAR network call and no API keys.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def demo_client(monkeypatch):
    """A TestClient with FINSIGHT_DEMO=1 forced on (offline fixtures)."""
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    # Reload modules so they pick up the env var at import time.
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture()
def offline_demo_client(monkeypatch):
    """Demo-mode client whose every network path raises if touched.

    Any live call the demo path still makes surfaces as a 500 with this
    fixture, so "demo mode is offline" is pinned by the suite rather than
    asserted in prose.
    """
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "demo mode attempted a network call — FINSIGHT_DEMO must be offline"
        )

    for client, methods in (
        (app_module._edgar, ("get_cik", "get_filings", "get_company_facts",
                             "get_filing_text", "search_filings")),
        (app_module._market, ("get_company_profile", "get_price_history",
                              "get_peers")),
        (app_module._transcripts, ("search_transcripts", "fetch_transcript_text")),
    ):
        for name in methods:
            monkeypatch.setattr(client, name, _forbidden)

    return TestClient(app_module.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_ok():
    from finsight.api.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "FinSight"
    assert "demo_tickers" in body
    assert isinstance(body["demo_tickers"], list)
    assert "AAPL" in body["demo_tickers"]


def test_health_reports_demo_mode(demo_client):
    body = demo_client.get("/health").json()
    assert body["demo_mode"] is True


# ---------------------------------------------------------------------------
# /kpis (demo path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_demo_kpis_shape(demo_client, ticker):
    resp = demo_client.get(f"/kpis/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == ticker
    # Real-shaped: core financials present and non-null.
    assert body["revenue"] is not None and body["revenue"] > 0
    assert body["net_income"] is not None
    assert body["gross_margin"] is not None
    assert 0 < body["gross_margin"] < 1
    assert body["shares_outstanding"] is not None and body["shares_outstanding"] > 0


# ---------------------------------------------------------------------------
# /dcf (demo path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA"])
def test_demo_dcf_shape(demo_client, ticker):
    resp = demo_client.get(f"/dcf/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == ticker
    assert body["intrinsic_value_per_share"] > 0
    assert body["enterprise_value_m"] > 0
    assert body["equity_value_m"] != 0
    assert 0 <= body["terminal_value_pct"] <= 1
    # Projection arrays come from the real DCFModel.
    assert isinstance(body["projected_revenues_m"], list)
    assert len(body["projected_revenues_m"]) == 5
    assert all(r > 0 for r in body["projected_revenues_m"])
    assert len(body["projected_fcfs_m"]) == 5
    assert body["assumptions"]["wacc"] > 0


def test_dcf_lists_its_defaults_and_its_adjustments(demo_client):
    """A DCF on a generic 10% WACC counts it as a model default, and lists the CapEx fade as an adjustment."""
    body = demo_client.get("/dcf/MSFT").json()
    assert body["defaulted_inputs"] == [
        "nwc_pct_of_revenue_change", "tax_rate", "terminal_growth_rate", "wacc"]
    assert body["adjustments"]["capex_as_pct_revenue"].startswith(
        "reported 34.9% of revenue in year 1, moving linearly to 10.3% "
        "(D&A less acquired-intangible amortization) by year 5")
    assert body["assumption_sources"]["capex_as_pct_revenue"].startswith(
        "reported CapEx / revenue, FY2026 (34.9%) in year 1")
    passed = demo_client.get("/dcf/MSFT?wacc=0.10&terminal_growth=0.03").json()
    assert passed["defaulted_inputs"] == ["nwc_pct_of_revenue_change", "tax_rate"]
    assert passed["intrinsic_value_per_share"] == pytest.approx(body["intrinsic_value_per_share"])


def test_demo_dcf_respects_n_years(demo_client):
    body = demo_client.get("/dcf/AAPL?n_years=7").json()
    assert len(body["projected_revenues_m"]) == 7
    assert body["assumptions"]["n_years"] == 7


def test_dcf_unknown_ticker_returns_404(monkeypatch):
    """
    An unknown ticker on the live path is a lookup failure → clean 404, NOT a
    500 with a raw exception string. Runs offline: get_cik is patched to raise
    the same ValueError EdgarClient raises for an unlisted ticker, and no demo
    fixture exists for 'ZZZZ' so the fallback does not mask the failure.
    """
    import finsight.api.app as app_module

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)

    def _no_such_ticker(ticker):
        raise ValueError(f"CIK not found for ticker: {ticker!r}.")

    monkeypatch.setattr(app_module._edgar, "get_cik", _no_such_ticker)

    client = TestClient(app_module.app)
    resp = client.get("/dcf/ZZZZ")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    # Friendly message, not the raw exception string.
    assert "ZZZZ" in detail
    assert "CIK not found" not in detail


# ---------------------------------------------------------------------------
# /sentiment (demo path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA"])
def test_demo_sentiment_shape(demo_client, ticker):
    resp = demo_client.get(f"/sentiment/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == ticker
    assert isinstance(body["results"], list)
    assert len(body["results"]) >= 1
    first = body["results"][0]
    for key in ("quarter", "overall_tone", "net_sentiment",
                "guidance_tone", "risk_mentions", "sentiment_drift"):
        assert key in first
    assert first["overall_tone"] in {"Bullish", "Bearish", "Neutral"}
    assert isinstance(first["net_sentiment"], (int, float))


def test_demo_sentiment_multiple_quarters(demo_client):
    body = demo_client.get("/sentiment/AAPL?n_transcripts=3").json()
    assert len(body["results"]) == 3


# ---------------------------------------------------------------------------
# /meta (demo path)
# ---------------------------------------------------------------------------

def test_demo_meta_shape(demo_client):
    resp = demo_client.get("/meta/AAPL")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["company_name"]
    assert body["sector"]
    assert body["current_price"] > 0
    assert body["mode"] == "demo"


# ---------------------------------------------------------------------------
# Demo mode is honestly tagged on EVERY JSON endpoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/meta/AAPL", "/filings/AAPL", "/kpis/AAPL",
                                  "/dcf/AAPL", "/sentiment/AAPL"])
def test_every_demo_json_response_is_tagged(demo_client, path):
    """A consumer must be able to tell demo output from live on every endpoint.

    Without the tag a caller could not distinguish a fixture from a real filing.
    """
    body = demo_client.get(path).json()
    assert body["mode"] == "demo", f"{path} returned an untagged demo response"


# ---------------------------------------------------------------------------
# Demo mode is offline for EVERY ticker, not only the bundled ones
# ---------------------------------------------------------------------------

TICKER_PATHS = ["/meta/{t}", "/filings/{t}", "/kpis/{t}", "/dcf/{t}",
                "/sentiment/{t}", "/report/{t}"]


@pytest.fixture()
def recorded_network(monkeypatch):
    """Demo-mode app with every outbound path recorded instead of sent.

    The identity reaches nobody, so a request that slipped out would be plain to see.
    """
    import requests
    from finsight.ingestion.transcripts import TranscriptFetcher

    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "FinSight demo demo@example.com")
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    sent = []

    def _record(label):
        def _call(*args, **kwargs):
            sent.append((label, args[:2], kwargs.get("headers")))
            raise requests.ConnectionError("recorded, not sent")
        return _call

    monkeypatch.setattr(requests.Session, "request", _record("http"))
    for name in ("get_company_profile", "get_price_history", "get_peers"):
        monkeypatch.setattr(app_module._market, name, _record(f"yahoo.{name}"))
    monkeypatch.setattr(TranscriptFetcher, "search_transcripts", _record("transcripts"))
    return app_module, sent


@pytest.mark.parametrize("path", TICKER_PATHS)
def test_demo_mode_refuses_a_ticker_it_does_not_bundle_without_any_request(recorded_network, path):
    """Demo mode refuses a ticker it does not bundle, names what it covers and how to run live, and requests nothing."""
    app_module, sent = recorded_network
    resp = TestClient(app_module.app, raise_server_exceptions=False).get(path.format(t="amzn"))
    assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])
    detail = resp.json()["detail"]
    assert detail.startswith("AMZN is not in the offline demo set (AAPL, MSFT, NVDA, GOOGL, TSLA)")
    assert "SEC_EDGAR_USER_AGENT" in detail and "FINSIGHT_DEMO" in detail
    assert sent == [], f"{path} made a request in demo mode: {sent}"


@pytest.mark.parametrize("path", TICKER_PATHS)
def test_demo_mode_still_serves_its_bundled_tickers_offline(recorded_network, path):
    app_module, sent = recorded_network
    resp = TestClient(app_module.app).get(path.format(t="AAPL"))
    assert resp.status_code == 200
    assert sent == []


# ---------------------------------------------------------------------------
# Tickers are validated before any third-party request
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_recorded(monkeypatch):
    """The LIVE app with SEC answered from a two-ticker map and Yahoo recorded."""
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    yahoo = []

    def _get_cik(t):
        known = {"AAPL": "0000320193", "BRK-B": "0001067983"}
        if t not in known:
            raise ValueError(f"CIK not found for ticker: {t!r}.")
        return known[t]

    def _record(name):
        def _call(*args, **kwargs):
            yahoo.append((name, args))
            raise RuntimeError("Yahoo is not reachable in tests")
        return _call

    monkeypatch.setattr(app_module._edgar, "get_cik", _get_cik)
    for name in ("get_company_profile", "get_price_history", "get_peers"):
        monkeypatch.setattr(app_module._market, name, _record(name))
    # Nothing in this fixture may leave the process: SEC beyond the ticker
    # map and the transcript scraper are recorded too.
    monkeypatch.setattr(app_module._edgar, "get_company_facts", _record("sec.companyfacts"))
    monkeypatch.setattr(app_module._edgar, "get_filings", _record("sec.filings"))
    from finsight.ingestion.transcripts import TranscriptFetcher
    monkeypatch.setattr(TranscriptFetcher, "_scrape_fool_transcripts", _record("fool.com"))
    return app_module, yahoo


@pytest.mark.parametrize("path", ["/meta/ZZZZ", "/report/ZZZZ"])
def test_an_unknown_ticker_is_refused_before_yahoo_sees_it(live_recorded, path):
    """/meta and /report check a ticker against SEC EDGAR's ticker map before any string reaches Yahoo Finance."""
    app_module, yahoo = live_recorded
    resp = TestClient(app_module.app, raise_server_exceptions=False).get(path)
    assert resp.status_code == 404, (resp.status_code, resp.text[:200])
    assert yahoo == [], f"Yahoo was asked about an unlisted ticker: {yahoo}"


def test_a_listed_ticker_still_reaches_yahoo(live_recorded):
    app_module, yahoo = live_recorded
    TestClient(app_module.app, raise_server_exceptions=False).get("/meta/AAPL")
    assert yahoo and yahoo[0][0] == "get_company_profile"


@pytest.mark.parametrize("raw", ["AAPL$", "A..B\\C", "ABCDEFGHIJKLMNOP", "-AAPL", "AA PL"])
@pytest.mark.parametrize("endpoint", ["meta", "kpis", "dcf", "sentiment", "report", "filings"])
def test_a_malformed_ticker_is_a_422_before_any_lookup(live_recorded, monkeypatch, endpoint, raw):
    app_module, yahoo = live_recorded
    sec = []
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: sec.append(t) or "0000000001")
    resp = TestClient(app_module.app, raise_server_exceptions=False).get(f"/{endpoint}/{raw}")
    assert resp.status_code == 422, (endpoint, raw, resp.status_code, resp.text[:200])
    assert "is not a ticker symbol" in resp.json()["detail"]
    assert sec == [] and yahoo == []


def test_share_class_tickers_are_well_formed(live_recorded):
    app_module, _ = live_recorded
    resp = TestClient(app_module.app, raise_server_exceptions=False).get("/meta/brk-b")
    assert resp.status_code != 422


# ---------------------------------------------------------------------------
# /filings (demo path)
# ---------------------------------------------------------------------------

def test_demo_filings_is_offline_and_honest(offline_demo_client):
    """/filings must honour FINSIGHT_DEMO instead of calling SEC EDGAR.

    It answers by declining: same shape as live, an empty index, and a reason.
    Bundling invented accession numbers would fabricate verifiable public
    records, so the demo declines rather than inventing.
    """
    resp = offline_demo_client.get("/filings/AAPL?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["mode"] == "demo"
    assert body["count"] == 0
    assert body["filings"] == []
    assert body["detail"]
    # Live shape preserved so a client needs no special-casing.
    for key in ("ticker", "cik", "form_type", "count", "filings"):
        assert key in body


def test_demo_filings_respects_form_type(offline_demo_client):
    body = offline_demo_client.get("/filings/MSFT?form_type=10-Q").json()
    assert body["form_type"] == "10-Q"


# ---------------------------------------------------------------------------
# /report (demo path)
# ---------------------------------------------------------------------------

def test_demo_report_is_offline(offline_demo_client):
    """/report must honour FINSIGHT_DEMO: no SEC EDGAR or yfinance call, so a demo run
    never shows a live quote."""
    resp = offline_demo_client.get("/report/AAPL")
    assert resp.status_code == 200
    text = resp.text
    assert "Apple Inc." in text
    assert "AAPL" in text


def test_demo_report_is_labelled_and_uses_fixture_price(offline_demo_client):
    text = offline_demo_client.get("/report/AAPL").text
    # Unmissable demo banner — the report is plain text, so it cannot carry a
    # "mode" JSON field like its siblings.
    assert "Demo mode" in text
    assert "FINSIGHT_DEMO" in text
    # The bundled, dated fixture price (Yahoo close 2026-10-30), not a live quote.
    assert "$344.63" in text
    assert "2026-10-30" in text


def test_demo_report_runs_the_real_engine(offline_demo_client):
    """The demo report is generated, not canned: the DCF section must carry the
    deterministic intrinsic value the real DCFModel produces for the bundled
    Apple FY2026 snapshot, re-derived independently in tests/hand_derivation.py
    ($165.31, with CapEx fading from reported to the steady state)."""
    from tests.hand_derivation import APPLE_FY2026, hand_dcf

    text = offline_demo_client.get("/report/AAPL").text
    assert "## DCF Valuation" in text
    assert f"${hand_dcf(**APPLE_FY2026):.2f}" == "$165.31"
    assert "$165.31" in text
    assert "DCF adjustment to a reported figure — capex_as_pct_revenue" in text
    assert "## Financial Summary" in text
    assert "## Earnings Sentiment Analysis" in text


def test_demo_report_omits_comps_rather_than_faking_peers(offline_demo_client):
    """Peer multiples need live market data. The demo omits the section and says
    so, instead of presenting synthetic peers as real."""
    text = offline_demo_client.get("/report/AAPL").text
    assert "## Comparable Company Analysis" not in text
    assert "comparable-company section is omitted" in text


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"])
def test_no_demo_report_rates_a_real_company(offline_demo_client, ticker):
    """A demo report carries no automated rating or price target: a fixed rule on a generic 10% WACC would be noise."""
    text = offline_demo_client.get(f"/report/{ticker}").text
    for phrase in ("Rating", "Price Target", "| **Sell", "| **Buy", "| **Hold",
                   "Investment Thesis", "upside", "downside"):
        assert phrase not in text, (ticker, phrase)
    assert "| DCF model value | Price | Model value vs price |" in text
    assert "A mechanical model output, not investment advice." in text
    assert "Management tone" not in text  # demo sentiment is synthetic


@pytest.mark.parametrize("ticker", ["MSFT", "NVDA", "GOOGL", "TSLA"])
def test_demo_report_works_for_every_bundled_ticker(offline_demo_client, ticker):
    text = offline_demo_client.get(f"/report/{ticker}").text
    assert "Equity Research Report" in text
    assert "Demo mode" in text


# ---------------------------------------------------------------------------
# /sentiment (LIVE path) — the demo fallback must survive a mid-loop failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [ValueError, RuntimeError])
def test_live_sentiment_falls_back_to_demo_when_analysis_fails(monkeypatch, exc):
    """A live-path failure raised AFTER the transcript loop starts must still
    serve the demo fixture, not a 500.

    Regression pin: `for t in transcripts:` rebound the name `t` from the ticker
    string to an EarningsTranscript, so every `except` handler below it called
    `demo_data.has_demo(<EarningsTranscript>)` -> AttributeError ('...' object
    has no attribute 'upper') -> HTTP 500, silently killing the demo fallback
    the README's data-modes table promises. Both the `except ValueError` and the
    broad `except Exception` handler routed through the same broken call, so
    both live-failure classes are pinned here.
    """
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module
    from finsight.ingestion.transcripts import EarningsTranscript

    importlib.reload(demo_data)
    importlib.reload(app_module)
    assert not demo_data.demo_enabled(), "this test must exercise the LIVE path"
    # Past the ticker check and the switch, or the loop this pins is never reached.
    monkeypatch.setenv("FINSIGHT_LIVE_TRANSCRIPTS", "1")
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000320193")

    transcript = EarningsTranscript(
        ticker="AAPL",
        company_name="Apple Inc.",
        quarter="Q4 2023",
        date="2023-11-02",
        url="https://example.invalid/transcript",
        full_text="Revenue grew this quarter and margins held.",
    )

    class _StubFetcher:
        """Stands in for the live TranscriptFetcher: succeeds, makes no network call."""

        def search_transcripts(self, ticker, limit=1):
            return [transcript]

        def fetch_transcript_text(self, tr):
            return tr.full_text

        def create_mock_transcript(self, ticker, quarter):
            return transcript

    monkeypatch.setattr(app_module, "TranscriptFetcher", _StubFetcher)

    def _boom(_transcript):
        raise exc("live sentiment analysis failed")

    monkeypatch.setattr(app_module._sentiment, "analyze_transcript", _boom)

    client = TestClient(app_module.app, raise_server_exceptions=False)
    resp = client.get("/sentiment/AAPL")

    assert resp.status_code == 200, (
        f"live-path {exc.__name__} did not fall back to demo: "
        f"{resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert exc.__name__ in body["fallback_reason"], body["fallback_reason"]
    assert body["mode"] == "demo"
    assert body["ticker"] == "AAPL"
    assert len(body["results"]) >= 1
    assert body["results"][0]["overall_tone"] in {"Bullish", "Bearish", "Neutral"}


# ---------------------------------------------------------------------------
# The model value and the price are dated separately
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,fye", [("AAPL", "2026-09-26"), ("MSFT", "2026-06-30")])
def test_demo_dcf_states_its_valuation_date(demo_client, ticker, fye):
    """The DCF is struck at the base fiscal year-end and set beside a later price,
    so the value carries its own date."""
    body = demo_client.get(f"/dcf/{ticker}").json()
    assert body["valuation_date"] == fye
    assert "not rolled forward" in body["valuation_date_basis"]


def test_demo_report_dates_the_model_value_and_the_price(offline_demo_client):
    text = offline_demo_client.get("/report/AAPL").text
    table = text.index("| DCF model value |")
    line = text[table:].split("\n\n")[1]
    assert line.startswith("**Valuation date:** 2026-09-26 (fiscal year-end of the base 10-K")
    assert "**Price date:** 2026-10-30 — 34 days apart" in line
    assert "not rolled forward" in line


# ---------------------------------------------------------------------------
# /kpis?period= is validated, bounded, and never silently the latest year
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("period", ["FY", "fy", "x" * 5000, "2025-9-27", "FY20255", "2025; DROP"],
                         ids=["FY", "fy", "5000-chars", "short-date", "FY20255", "injection"])
def test_a_malformed_period_is_a_bounded_422(demo_client, period):
    """A malformed period is a bounded 422 that does not echo the input, and "FY" is not read as the latest year."""
    r = demo_client.get("/kpis/AAPL", params={"period": period})
    assert r.status_code == 422
    assert len(r.text) < 300
    assert len(period) < 5 or period not in r.text   # the input is not echoed


@pytest.mark.parametrize("period,fy", [("2022", "FY2022"), ("FY2023", "FY2023"),
                                        ("2025-09-27", "FY2025"), ("", "FY2026")])
def test_a_wellformed_period_is_served(demo_client, period, fy):
    assert demo_client.get("/kpis/AAPL", params={"period": period}).json()["period"] == fy


def test_the_library_refuses_a_bare_fy():
    from finsight.api import demo_data
    with pytest.raises(ValueError, match="names no fiscal year"):
        demo_data.demo_kpis("AAPL", "FY")


def test_dcf_honours_period(demo_client):
    """A year outside the snapshot is a 404, never the latest year at a 200."""
    body = demo_client.get("/dcf/AAPL", params={"period": "2022"}).json()
    assert body["fiscal_year"] == "FY2022" and body["valuation_date"] == "2022-09-24"
    assert demo_client.get("/dcf/AAPL", params={"period": "2020"}).status_code == 404
    assert demo_client.get("/dcf/AAPL", params={"period": "FY"}).status_code == 422
    assert demo_client.get("/dcf/AAPL").json()["fiscal_year"] == "FY2026"


# ---------------------------------------------------------------------------
# /filings validates form_type, as /kpis validates period
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["<script>alert(1)</script>", "10-K<b>", "x" * 16, "", " 10-K",
                                   "10-K\n", "%3Cscript%3E"])
def test_filings_refuses_a_form_type_that_is_not_one_and_does_not_echo_it(monkeypatch, value):
    from finsight.api.app import app

    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    response = TestClient(app).get("/filings/AAPL", params={"form_type": value})
    assert response.status_code == 422
    assert "form_type must be an SEC form type" in response.json()["detail"]
    for echoed in ("<", "alert", "x" * 16, "%3C"):
        assert echoed not in response.text


@pytest.mark.parametrize("value", ["10-K", "10-Q", "8-K", "10-K/A", "DEF 14A", "S-1", "20-F"])
def test_filings_takes_the_form_types_sec_uses(monkeypatch, value):
    from finsight.api.app import app

    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    response = TestClient(app).get("/filings/AAPL", params={"form_type": value})
    assert response.status_code == 200
    assert response.json()["form_type"] == value


@pytest.mark.parametrize("raw", ["%3Cscript%3E", "ZZ%20QQ%20XX"])
@pytest.mark.parametrize("endpoint", ["meta", "kpis", "dcf", "sentiment", "report", "filings"])
def test_a_malformed_ticker_is_not_echoed(live_recorded, endpoint, raw):
    from urllib.parse import unquote

    app_module, _ = live_recorded
    resp = TestClient(app_module.app, raise_server_exceptions=False).get(f"/{endpoint}/{raw}")
    assert resp.status_code == 422
    assert unquote(raw) not in resp.text


@pytest.mark.parametrize("period", ["2025-13-45", "2025-02-30", "2025-00-10", "0000-01-01"])
@pytest.mark.parametrize("path", ["/kpis/AAPL", "/dcf/AAPL"])
def test_a_date_no_calendar_has_is_the_documented_422(demo_client, path, period):
    """?period=2025-13-45 is a 422, as the README's "anything else is a 422" says."""
    r = demo_client.get(path, params={"period": period})
    assert r.status_code == 422
    assert period not in r.text


def test_a_real_date_that_is_not_a_year_end_stays_a_404(demo_client):
    assert demo_client.get("/kpis/AAPL", params={"period": "2025-03-15"}).status_code == 404
    assert demo_client.get("/kpis/AAPL", params={"period": "2025-09-27"}).status_code == 200

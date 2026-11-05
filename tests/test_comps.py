"""
Tests for comparable company analysis.

Two behaviours are pinned here because either could fail silently:

  * **Every advertised multiple contributes.** EV/EBITDA and P/E are in the peer
    stats and the research report's multiple table, and the implied valuation
    applies them as well as the two revenue-based multiples.
  * **No multiple is invented.** On a peer-fetch failure, a failed peer is left
    out and named.

Runs fully offline — the market-data client is stubbed.
"""

import re

import pytest

from finsight.ingestion.market_data import CompanyProfile
from finsight.models.comps import CompsAnalysis

ADVERTISED_MULTIPLES = ["ev_revenue", "ev_ebitda", "pe", "ps"]


def _profile(ticker: str, *, revenue_ttm=50_000e6, ebitda_ttm=15_000e6,
             net_margin=0.20, pe=25.0, ev_ebitda=18.0, ps=6.0) -> CompanyProfile:
    """A fully populated profile — the case where nothing should be synthetic."""
    return CompanyProfile(
        ticker=ticker,
        name=f"{ticker} Inc.",
        sector="Technology",
        industry="Software",
        market_cap=600e9,
        enterprise_value=620e9,
        pe_ratio=pe,
        pb_ratio=9.0,
        ps_ratio=ps,
        ev_ebitda=ev_ebitda,
        debt_to_equity=0.5,
        roe=0.35,
        roa=0.18,
        gross_margin=0.60,
        operating_margin=0.30,
        net_margin=net_margin,
        revenue_ttm=revenue_ttm,
        ebitda_ttm=ebitda_ttm,
        free_cash_flow=12_000e6,
        beta=1.1,
        dividend_yield=0.005,
    )


class _StubMarketData:
    """Serves canned profiles; optionally fails for named tickers."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()

    def get_company_profile(self, ticker: str) -> CompanyProfile:
        if ticker in self.failing:
            raise RuntimeError(f"simulated market-data outage for {ticker}")
        # Vary the peers a little so median != mean.
        bump = 1.0 + (len(ticker) % 3) * 0.1
        return _profile(ticker, pe=25.0 * bump, ev_ebitda=18.0 * bump, ps=6.0 * bump)

    def get_peers(self, ticker: str) -> list[str]:
        return ["PEERA", "PEERB", "PEERC"]


class _KPIs:
    """Minimal stand-in for ExtractedKPIs ($M), which is what analyze() reads."""

    def __init__(self, revenue=50_000.0, ebitda=15_000.0, net_income=10_000.0):
        self.revenue = revenue
        self.ebitda = ebitda
        self.net_income = net_income


# ---------------------------------------------------------------------------
# The multiple gate
# ---------------------------------------------------------------------------

def test_every_advertised_multiple_contributes_to_a_valuation():
    """All four multiples the report displays must reach implied_valuations."""
    comps = CompsAnalysis(_StubMarketData())
    result = comps.analyze("TGT", peers=["PEERA", "PEERB", "PEERC"])

    missing = [m for m in ADVERTISED_MULTIPLES if m not in result.implied_valuations]
    assert not missing, (
        f"multiples advertised in the report but excluded from the valuation: {missing}"
    )
    for mult in ADVERTISED_MULTIPLES:
        assert result.implied_valuations[mult]["median"] > 0


def test_multiple_basis_covers_every_row_the_report_prints():
    """The report's multiple table and the valuation gate must not drift apart."""
    assert set(CompsAnalysis._MULTIPLE_BASIS) == set(ADVERTISED_MULTIPLES)


def test_ev_and_equity_values_are_not_mixed():
    """P/S implies an EQUITY value; folding it into 'implied enterprise value'
    is dimensionally wrong. The two ranges are computed separately."""
    comps = CompsAnalysis(_StubMarketData())
    result = comps.analyze("TGT", peers=["PEERA", "PEERB", "PEERC"])

    ev_sources = {m for m, v in result.implied_valuations.items() if v["basis"] == "ev"}
    eq_sources = {m for m, v in result.implied_valuations.items() if v["basis"] == "equity"}
    assert ev_sources == {"ev_revenue", "ev_ebitda"}
    assert eq_sources == {"pe", "ps"}

    assert result.implied_ev_m["mid"] > 0
    assert result.implied_equity_m["mid"] > 0
    # Different bases applied to different anchors → genuinely different ranges.
    assert result.implied_ev_m["mid"] != result.implied_equity_m["mid"]


def test_multiple_is_skipped_only_when_its_anchor_is_missing():
    """A multiple drops out for a data reason (no EBITDA), not a structural one."""
    comps = CompsAnalysis(_StubMarketData())
    no_ebitda = {
        "TGT": _profile("TGT", ebitda_ttm=None),
        "PEERA": _profile("PEERA"),
    }
    comps.mdc = type("M", (), {
        "get_company_profile": staticmethod(lambda t: no_ebitda[t]),
        "get_peers": staticmethod(lambda t: ["PEERA"]),
    })()
    result = comps.analyze("TGT", peers=["PEERA"])

    assert "ev_ebitda" not in result.implied_valuations
    assert "ev_revenue" in result.implied_valuations


def test_kpis_supply_the_anchors_when_the_profile_cannot():
    """Extracted KPIs carry EBITDA/net income, so passing them lets EV/EBITDA
    and P/E contribute even when the market profile is thin."""
    thin = {
        "TGT": _profile("TGT", ebitda_ttm=None, net_margin=None),
        "PEERA": _profile("PEERA"),
        "PEERB": _profile("PEERB"),
    }
    comps = CompsAnalysis(type("M", (), {
        "get_company_profile": staticmethod(lambda t: thin[t]),
        "get_peers": staticmethod(lambda t: ["PEERA", "PEERB"]),
    })())

    without = comps.analyze("TGT", peers=["PEERA", "PEERB"])
    assert "ev_ebitda" not in without.implied_valuations
    assert "pe" not in without.implied_valuations

    with_kpis = comps.analyze("TGT", peers=["PEERA", "PEERB"], kpis=_KPIs())
    assert with_kpis.implied_valuations["ev_ebitda"]["median"] > 0
    assert with_kpis.implied_valuations["pe"]["median"] > 0


# ---------------------------------------------------------------------------
# Nothing is invented: a failed fetch is left out and named
# ---------------------------------------------------------------------------

def test_the_hash_seeded_multiples_are_gone():
    """A hash of a real ticker's name must not make a number, anywhere. These
    replace the tests that pinned the opt-in stand-in's tagging."""
    from pathlib import Path

    assert not hasattr(CompsAnalysis, "_mock_multiples")
    with pytest.raises(TypeError):
        CompsAnalysis(None, synthetic_fallback=True)
    package = Path(__file__).parents[1] / "finsight"
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for stale in ("hashlib.md5", "default_rng", "random.", "_mock_multiples", "synthetic_multiples"):
            assert stale not in text, f"{path.name}: {stale}"


def test_total_outage_names_every_symbol_including_the_target():
    result = CompsAnalysis(_StubMarketData(failing={"TGT", "PEERA", "PEERB"})).analyze(
        "TGT", peers=["PEERA", "PEERB"])
    assert sorted(result.unavailable) == ["PEERA", "PEERB", "TGT"]
    assert result.peer_multiples.empty and result.target_multiples == {}


def test_no_market_client_names_every_symbol_and_invents_nothing():
    result = CompsAnalysis(None).analyze("TGT", peers=["PEERA"])
    assert sorted(result.unavailable) == ["PEERA", "TGT"]
    assert result.implied_ev_m == {"low": 0.0, "mid": 0.0, "high": 0.0}


def test_report_shows_both_value_bases_when_peers_are_real():
    from finsight.report.generator import ReportGenerator

    comps = CompsAnalysis(_StubMarketData())
    result = comps.analyze("TGT", peers=["PEERA", "PEERB"])

    gen = ReportGenerator()
    markdown = gen.to_markdown(
        gen.generate(ticker="TGT", comps_result=result, company_name="TGT Inc.")
    )
    # Both value bases are reported, and never conflated into one number.
    assert "Implied Enterprise Value (EV multiples)" in markdown
    assert "Implied Equity Value (price multiples)" in markdown


# ---------------------------------------------------------------------------
# Loss-making peers: a negative multiple is not a price
# ---------------------------------------------------------------------------

class _LossMakingPeers:
    """TSLA's curated peer set with RIVN / NIO (and optionally LI) reporting
    negative EV/EBITDA, as loss-making companies do."""

    def __init__(self, ev_ebitda: dict[str, float]) -> None:
        self.ev_ebitda = ev_ebitda

    def get_company_profile(self, ticker: str) -> CompanyProfile:
        return _profile(ticker, ev_ebitda=self.ev_ebitda.get(ticker, 20.0))

    def get_peers(self, ticker: str) -> list[str]:
        return list(self.ev_ebitda)


def _tsla_comps(ev_ebitda: dict[str, float]):
    return CompsAnalysis(_LossMakingPeers(ev_ebitda)).analyze(
        "TSLA", peers=list(ev_ebitda), kpis=_KPIs(ebitda=9_385.0))


def test_negative_peer_multiples_are_left_out_of_the_peer_median():
    """A loss-maker stays out of the peer median: 9.0x of the three peers with positive EBITDA, not 7.0x with RIVN −4x and NIO −3x."""
    result = _tsla_comps({"RIVN": -4.0, "NIO": -3.0, "GM": 7.0, "F": 9.0, "LI": 11.0})

    assert result.peer_stats.loc["ev_ebitda", "median"] == pytest.approx(9.0)
    assert result.peer_stats.loc["ev_ebitda", "mean"] == pytest.approx(9.0)
    assert result.implied_valuations["ev_ebitda"]["median"] == pytest.approx(9.0 * 9_385.0)
    excluded = {(e["ticker"], e["multiple"]): e for e in result.excluded_multiples}
    assert set(excluded) == {("RIVN", "ev_ebitda"), ("NIO", "ev_ebitda")}
    assert excluded[("RIVN", "ev_ebitda")]["value"] == -4.0
    assert "negative" in excluded[("RIVN", "ev_ebitda")]["reason"]
    # The raw figure is still shown, just not counted.
    assert result.peer_multiples.loc["RIVN", "ev_ebitda"] == -4.0


def test_a_majority_of_loss_makers_never_implies_a_negative_value():
    """Three loss-making peers made the median −1.0x and the implied EV from
    EV/EBITDA −$9,385M."""
    result = _tsla_comps({"RIVN": -4.0, "NIO": -3.0, "LI": -1.0, "GM": 7.0, "F": 9.0})

    assert result.peer_stats.loc["ev_ebitda", "median"] == pytest.approx(8.0)
    for implied in result.implied_valuations.values():
        assert implied["median"] > 0 and implied["mean"] > 0
    assert {e["ticker"] for e in result.excluded_multiples} == {"RIVN", "NIO", "LI"}


def test_zero_and_non_finite_multiples_are_excluded_too():
    result = _tsla_comps({"RIVN": 0.0, "NIO": float("inf"), "GM": 7.0, "F": 9.0})

    assert result.peer_stats.loc["ev_ebitda", "median"] == pytest.approx(8.0)
    reasons = {e["ticker"]: e["reason"] for e in result.excluded_multiples}
    assert reasons == {"RIVN": "zero", "NIO": "non-finite"}


def test_no_usable_peer_multiple_means_no_implied_value_from_it():
    result = _tsla_comps({"RIVN": -4.0, "NIO": -3.0})

    assert "ev_ebitda" not in result.implied_valuations
    assert all(v > 0 for imp in result.implied_valuations.values()
               for k, v in imp.items() if k in ("median", "mean"))


def test_report_names_the_excluded_peers_and_prints_no_nan():
    from finsight.report.generator import ReportGenerator

    gen = ReportGenerator()
    markdown = gen.to_markdown(gen.generate(
        ticker="TSLA", company_name="Tesla, Inc.",
        comps_result=_tsla_comps({"RIVN": -4.0, "NIO": -3.0}),
    ))
    assert "Excluded from the peer statistics" in markdown
    assert "- RIVN ev/ebitda -4.0x — negative" in markdown
    assert "- NIO ev/ebitda -3.0x — negative" in markdown
    # No usable peer EV/EBITDA: the table says N/A, not "nanx" or "-3.5x".
    assert "| ev/ebitda | 20.0x | N/A | Enterprise value |" in markdown
    assert not re.search(r"\bnanx?\b", markdown)


# ---------------------------------------------------------------------------
# Curated peer sets name only listed companies
# ---------------------------------------------------------------------------

# Symbols that do not trade, each with why: a peer list naming one can only fail its
# profile fetch and be left out.
KNOWN_DELISTED = {
    "TWTR": "Twitter, taken private 2022-10",
    "FB": "renamed META 2022-06",
    "ATVI": "acquired by Microsoft 2023-10",
    "XLNX": "acquired by AMD 2022-02",
    "VMW": "acquired by Broadcom 2023-11",
    "SPLK": "acquired by Cisco 2024-03",
    "CERN": "acquired by Oracle 2022-06",
}
CURATED = ["AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA", "JPM", "TSLA", "JNJ"]


@pytest.mark.parametrize("ticker", CURATED)
def test_no_curated_peer_is_a_known_delisted_ticker(ticker):
    """META's peers included TWTR, delisted in 2022."""
    from finsight.ingestion.market_data import MarketDataClient

    peers = MarketDataClient().get_peers(ticker)  # curated: no network
    assert len(peers) == 5 and ticker not in peers
    assert not set(peers) & set(KNOWN_DELISTED), set(peers) & set(KNOWN_DELISTED)


def test_the_curated_table_is_the_one_tested():
    from finsight.ingestion.market_data import CURATED_PEERS

    assert sorted(CURATED_PEERS) == sorted(CURATED)


def test_an_uncurated_ticker_gets_no_peers_and_no_request(monkeypatch):
    """get_peers has no sector or ETF fallback, and makes no request for a ticker without curated peers."""
    from finsight.ingestion.market_data import MarketDataClient

    client = MarketDataClient()

    def _no_network(t):
        raise AssertionError("get_peers made a market-data request")
    monkeypatch.setattr(client, "get_company_profile", _no_network)
    assert client.get_peers("ZZZZ") == []
    assert "falls back to sector ETF" not in MarketDataClient.get_peers.__doc__


def test_ev_revenue_is_never_a_relabelled_p_s():
    """With no revenue from Yahoo, EV/Revenue silently fell back to P/S."""
    c = CompsAnalysis(None)
    m = c._profile_to_multiples(_profile("X", revenue_ttm=None, ps=6.0))
    assert "ev_revenue" not in m and m["ps"] == 6.0


def test_ev_ebitda_is_not_applied_to_a_depreciation_only_ebitda():
    """Peers' Yahoo EV/EBITDA (full D&A) is not applied to a target whose 10-K EBITDA is depreciation-only, which would understate the implied EV."""
    from types import SimpleNamespace
    from finsight.report.generator import ReportGenerator

    k = SimpleNamespace(revenue=50_000.0, ebitda=15_000.0, net_income=10_000.0, period="FY2026",
                        provenance={"limitations": {"depreciation_amortization": "D&A is ..."}})
    result = CompsAnalysis(_StubMarketData()).analyze("TGT", peers=["PEERA", "PEERB"], kpis=k)
    assert "ev_ebitda" not in result.implied_valuations
    assert "different bases" in result.skipped_multiples["ev_ebitda"]
    md = ReportGenerator().to_markdown(ReportGenerator().generate(ticker="TGT", comps_result=result))
    assert "| ev/ebitda |" in md and "Not applied (see below)" in md
    assert "EV/EBITDA not applied: the target's EBITDA is operating income + PP&E depreciation only" in md
    # A full-D&A target still uses it.
    full = SimpleNamespace(revenue=50_000.0, ebitda=15_000.0, net_income=10_000.0, period="FY2026",
                           provenance={"limitations": {}})
    assert "ev_ebitda" in CompsAnalysis(_StubMarketData()).analyze(
        "TGT", peers=["PEERA", "PEERB"], kpis=full).implied_valuations



# ---------------------------------------------------------------------------
# By default nothing is invented: no data -> left out / unavailable
# ---------------------------------------------------------------------------

def test_a_failed_fetch_is_left_out_not_invented():
    result = CompsAnalysis(_StubMarketData(failing={"PEERB"})).analyze("TGT", peers=["PEERA", "PEERB"])
    assert "PEERB" not in result.peer_multiples.index
    assert result.unavailable == {"PEERB": "market data not retrieved (RuntimeError)"}


def test_live_report_with_yahoo_down_says_comps_are_unavailable(monkeypatch):
    """With Yahoo rate-limited the live /report prints no implied value and no invented target P/E."""
    import importlib
    from fastapi.testclient import TestClient
    import finsight.api.app as app_module
    from finsight.data import companyfacts_snapshot

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000320193")
    monkeypatch.setattr(app_module._edgar, "get_company_facts", lambda cik: companyfacts_snapshot("AAPL"))
    monkeypatch.setattr(app_module, "_transcript_sentiments", lambda t, n: [])
    # Yahoo is blocked suite-wide (tests/conftest.py): every profile fails.
    text = TestClient(app_module.app).get("/report/AAPL").text
    comps = text[text.index("## Comparable Company Analysis"):].split("\n## ")[0]
    assert "Unavailable: peer market data could not be retrieved" in comps
    assert "No comparable-company figure is shown." in comps
    assert "Implied Enterprise Value" not in comps
    assert "x |" not in comps            # no multiple table at all


# ---------------------------------------------------------------------------
# Priced in one currency, reported in another: left out and named
# ---------------------------------------------------------------------------

class _CurrencyStub(_StubMarketData):
    """TSM as Yahoo describes it: USD price and EV, TWD revenue."""

    CURRENCIES = {"TSM": ("USD", "TWD"), "NIO": ("USD", "CNY"), "LI": ("usd", "CNY"),
                  "PEERA": ("USD", "USD"), "PEERB": ("USD", "usd"), "TGT": ("USD", "USD")}

    def get_company_profile(self, ticker):
        p = super().get_company_profile(ticker)
        p.currency, p.financial_currency = self.CURRENCIES.get(ticker, (None, None))
        if p.financial_currency in ("TWD", "CNY"):
            p.revenue_ttm *= 30      # the same revenue, in a currency worth 1/30 of a dollar
        return p


def test_a_peer_reporting_in_another_currency_is_left_out_and_named():
    peers = ["PEERA", "PEERB", "TSM", "NIO", "LI"]
    result = CompsAnalysis(_CurrencyStub()).analyze("TGT", peers=peers)
    assert set(result.peer_multiples.index) == {"PEERA", "PEERB"}
    assert set(result.unavailable) == {"TSM", "NIO", "LI"}
    assert result.unavailable["TSM"].startswith("priced in USD but reports in TWD")
    assert result.unavailable["LI"].startswith("priced in USD but reports in CNY")
    # Only same-currency peers set the median: a mixed peer's EV/revenue is 1/30 of it.
    clean = CompsAnalysis(_CurrencyStub()).analyze("TGT", peers=["PEERA", "PEERB"])
    assert result.peer_stats.loc["ev_revenue", "median"] == clean.peer_stats.loc["ev_revenue", "median"]


def test_a_currency_mismatch_invents_no_multiples():
    result = CompsAnalysis(_CurrencyStub()).analyze("TGT", peers=["PEERA", "TSM"])
    assert "TSM" not in result.peer_multiples.index and "TSM" in result.unavailable


def test_a_profile_without_both_currencies_is_used_as_before():
    """Yahoo may omit financialCurrency: untestable, so unchanged."""
    result = CompsAnalysis(_CurrencyStub()).analyze("TGT", peers=["PEERA", "UNSTATED"])
    assert "UNSTATED" in result.peer_multiples.index
    assert result.unavailable == {}


def test_the_report_names_a_peer_left_out_for_its_currency():
    from finsight.report.generator import ReportGenerator
    result = CompsAnalysis(_CurrencyStub()).analyze("TGT", peers=["PEERA", "PEERB", "TSM"])
    gen = ReportGenerator()
    text = gen.to_markdown(gen.generate("TGT", comps_result=result))
    assert "Left out (no usable market data): TSM — priced in USD but reports in TWD" in text


def test_the_market_data_client_reads_both_currencies(monkeypatch):
    from types import SimpleNamespace
    from finsight.ingestion.market_data import MarketDataClient
    info = {"longName": "Taiwan Semi", "currency": "USD", "financialCurrency": "TWD",
            "totalRevenue": 3.8e12, "enterpriseValue": 1.0e12, "marketCap": 1.0e12}
    monkeypatch.setattr(MarketDataClient, "_get_ticker", lambda self, s: SimpleNamespace(info=info))
    p = MarketDataClient().get_company_profile("TSM")
    assert (p.currency, p.financial_currency) == ("USD", "TWD")


# ---------------------------------------------------------------------------
# Peers are a hand-picked list, and a report without comps says why
# ---------------------------------------------------------------------------

def test_a_ticker_with_no_curated_peers_says_why_its_report_has_no_comps(monkeypatch):
    """A report without curated peers says why it has no comps, and does not credit Yahoo for the peers."""
    from tests.test_live_path import apple_facts

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    import importlib
    import finsight.api.demo_data as demo_data
    import finsight.api.app as live
    from fastapi.testclient import TestClient

    importlib.reload(demo_data)
    importlib.reload(live)

    def yahoo_never_asked(*_a, **_k):
        raise RuntimeError("Yahoo is down")

    monkeypatch.setattr(live._edgar, "get_cik", lambda t: "0000104169")
    monkeypatch.setattr(live._edgar, "get_company_facts", lambda cik: apple_facts())
    for name in ("get_company_profile", "get_price_history", "get_splits"):
        monkeypatch.setattr(live._market, name, yahoo_never_asked)
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    text = TestClient(live.app).get("/report/WMT").text
    comps = text[text.index("## Comparable Company Analysis"):]
    assert comps.startswith(
        "## Comparable Company Analysis\n\nUnavailable: WMT has no curated peer set. "
        "FinSight's peers are a hand-picked list for AAPL, AMZN, GOOGL, JNJ, JPM, META, MSFT, "
        "NVDA, TSLA only; it does not look peers up. No comparable-company figure is shown.")


def test_a_failed_peer_analysis_says_so():
    from finsight.report.generator import ReportGenerator

    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate("TGT", comps_unavailable="The peer analysis failed (KeyError)."))
    assert "Unavailable: The peer analysis failed (KeyError). No comparable-company figure" in md


def test_a_report_never_asked_for_comps_has_no_section():
    """The demo report omits the section and its banner says why."""
    from finsight.report.generator import ReportGenerator

    gen = ReportGenerator()
    assert "Comparable Company" not in gen.to_markdown(gen.generate("TGT"))


def test_the_readme_states_the_curated_peer_list_and_does_not_credit_yahoo_for_it():
    from pathlib import Path
    from finsight.ingestion.market_data import CURATED_PEERS

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert len(CURATED_PEERS) == 9
    assert f"nine tickers ({', '.join(sorted(CURATED_PEERS))})" in readme
    row = next(line for line in readme.splitlines() if line.startswith("| **Yahoo Finance**"))
    assert "Prices, company profile, peers' multiples" in row
    assert "not the peer list" in row


# ---------------------------------------------------------------------------
# An empty Yahoo profile is no data, and is named
# ---------------------------------------------------------------------------

class _EmptyProfiles(_StubMarketData):
    """Yahoo answering with empty info: MarketDataClient returns a profile with
    the ticker for a name and nothing else."""

    def __init__(self, empty):
        super().__init__()
        self.empty = set(empty)

    def get_company_profile(self, ticker):
        if ticker not in self.empty:
            return super().get_company_profile(ticker)
        return CompanyProfile(
            ticker=ticker, name=ticker, sector="", industry="", market_cap=0, enterprise_value=0,
            pe_ratio=None, pb_ratio=None, ps_ratio=None, ev_ebitda=None, debt_to_equity=None,
            roe=None, roa=None, gross_margin=None, operating_margin=None, net_margin=None,
            revenue_ttm=None, ebitda_ttm=None, free_cash_flow=None, beta=None, dividend_yield=None)


NO_MULTIPLE = "market data retrieved, but its profile holds no multiple"


def test_every_empty_profile_is_named_and_no_figure_prints():
    """With a client returning empty profiles, each peer is named in `unavailable` and the report prints no row of N/A."""
    from finsight.report.generator import ReportGenerator

    result = CompsAnalysis(_EmptyProfiles({"TGT", "PEERA", "PEERB"})).analyze("TGT", peers=["PEERA", "PEERB"])
    assert result.unavailable == {"TGT": NO_MULTIPLE, "PEERA": NO_MULTIPLE, "PEERB": NO_MULTIPLE}
    assert result.peer_multiples.empty
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate("TGT", comps_result=result))
    comps = md[md.index("## Comparable Company Analysis"):]
    assert "N/A — N/A — N/A" not in md
    assert f"Unavailable: peer market data could not be retrieved (PEERA: {NO_MULTIPLE}" in comps
    assert "TGT:" in comps
    assert "No comparable-company figure is shown." in comps


def test_one_empty_peer_is_left_out_and_named():
    from finsight.report.generator import ReportGenerator

    result = CompsAnalysis(_EmptyProfiles({"PEERB"})).analyze("TGT", peers=["PEERA", "PEERB"])
    assert result.unavailable == {"PEERB": NO_MULTIPLE}
    assert list(result.peer_multiples.index) == ["PEERA"]
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate("TGT", comps_result=result))
    assert f"Left out (no usable market data): PEERB — {NO_MULTIPLE}." in md


def test_a_peer_with_any_multiple_still_counts():
    result = CompsAnalysis(_StubMarketData()).analyze("TGT", peers=["PEERA", "PEERB"])
    assert result.unavailable == {}
    assert list(result.peer_multiples.index) == ["PEERA", "PEERB"]


def test_peer_multiples_with_nothing_to_apply_them_to_say_so():
    """Peers answered and the target's profile did not, with no KPIs to anchor on."""
    from finsight.report.generator import ReportGenerator

    result = CompsAnalysis(_EmptyProfiles({"TGT"})).analyze("TGT", peers=["PEERA", "PEERB"])
    gen = ReportGenerator()
    md = gen.to_markdown(gen.generate("TGT", comps_result=result))
    assert "N/A — N/A — N/A" not in md
    assert "**Implied Enterprise Value (EV multiples):** not computed:" in md
    assert f"TGT — {NO_MULTIPLE}" in md


# ---------------------------------------------------------------------------
# A financial company gets no implied enterprise value
# ---------------------------------------------------------------------------

class _Banks(_StubMarketData):
    """JPM and its curated peers, sectors as Yahoo Finance names them."""

    def __init__(self, target_sector="Financial Services", peer_sector="Financial Services",
                 failing=None) -> None:
        super().__init__(failing)
        self.target_sector, self.peer_sector = target_sector, peer_sector

    def get_company_profile(self, ticker):
        profile = super().get_company_profile(ticker)
        profile.sector = self.target_sector if ticker == "JPM" else self.peer_sector
        return profile

    def get_peers(self, ticker):
        return ["BAC", "WFC", "GS", "MS", "C"]


def test_a_bank_gets_no_implied_enterprise_value_and_is_told_why():
    result = CompsAnalysis(_Banks()).analyze("JPM", kpis=_KPIs())
    assert result.implied_ev_m == {"low": 0.0, "mid": 0.0, "high": 0.0}
    assert "ev_revenue" not in result.implied_valuations
    assert "ev_ebitda" not in result.implied_valuations
    assert "financial company" in result.ev_refused
    for multiple in ("ev_revenue", "ev_ebitda"):
        assert result.skipped_multiples[multiple].startswith("not applied: JPM is a financial company")


def test_a_bank_keeps_its_price_multiples():
    result = CompsAnalysis(_Banks()).analyze("JPM", kpis=_KPIs())
    assert {"pe", "ps"} <= set(result.implied_valuations)
    assert result.implied_equity_m["mid"] > 0


def test_the_gate_holds_when_the_target_s_own_profile_failed():
    result = CompsAnalysis(_Banks(failing={"JPM"})).analyze("JPM", kpis=_KPIs())
    assert result.ev_refused and result.implied_ev_m["mid"] == 0.0


def test_a_blank_target_sector_falls_to_its_peers():
    assert CompsAnalysis(_Banks(target_sector="")).analyze("JPM", kpis=_KPIs()).ev_refused
    assert not CompsAnalysis(_Banks(target_sector="", peer_sector="Technology")).analyze(
        "JPM", kpis=_KPIs()).ev_refused


def test_a_company_outside_financials_is_untouched():
    result = CompsAnalysis(_Banks(target_sector="Technology")).analyze("JPM", kpis=_KPIs())
    assert result.ev_refused is None and result.implied_ev_m["mid"] > 0
    assert "ev_revenue" not in result.skipped_multiples
    plain = CompsAnalysis(_StubMarketData()).analyze("TGT", kpis=_KPIs())
    assert plain.ev_refused is None and "ev_revenue" in plain.implied_valuations


def test_report_jpm_refuses_the_enterprise_value_and_says_why(monkeypatch):
    """/report/JPM prints no "Implied Enterprise Value" from EV multiples."""
    from fastapi.testclient import TestClient
    import finsight.api.app as live
    from tests.test_live_path import apple_facts

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setattr(live._edgar, "get_cik", lambda t: "0000019617")
    monkeypatch.setattr(live._edgar, "get_company_facts", lambda cik: apple_facts())
    monkeypatch.setattr(live, "_transcript_sentiments", lambda t, n: [])
    banks = _Banks()
    monkeypatch.setattr(live._market, "get_company_profile", banks.get_company_profile)
    monkeypatch.setattr(live._comps, "mdc", banks)

    def no_price(*_a, **_k):
        raise RuntimeError("Yahoo is down")

    monkeypatch.setattr(live._market, "get_price_history", no_price)
    monkeypatch.setattr(live._market, "get_splits", no_price)
    text = TestClient(live.app).get("/report/JPM").text
    comps = text[text.index("## Comparable Company Analysis"):].split("\n## ")[0]
    assert "- **Implied Enterprise Value (EV multiples):** not computed: JPM is a financial company" in comps
    assert "Price multiples still apply." in comps
    assert "- **Implied Equity Value (price multiples):** $" in comps
    assert "| ev/revenue |" in comps and "Not applied (see below)" in comps
    assert "- **Implied Enterprise Value (EV multiples):** $" not in comps

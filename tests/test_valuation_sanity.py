"""
Valuation sanity regression tests.

What these guard:

  * DCF growth. A trailing YoY growth figure compounded straight into the projection
    would give an absurd FCF stream, so growth is clamped to <=40% and decays linearly
    to the terminal rate. Every demo ticker's intrinsic value must land in a sane band
    of its fixture price, and NVDA's enterprise value far below $10T.

  * /dcf params. ``wacc`` and ``terminal_growth`` must be applied, not only validated
    and echoed: via TestClient in FINSIGHT_DEMO mode, a higher WACC produces a strictly
    LOWER intrinsic value.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from tests.hand_derivation import ALPHABET_FY2025, MICROSOFT_FY2026, hand_dcf

DEMO_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]

# Sane band for intrinsic-value / fixture-price ratio. The upper bound (3x)
# catches the bug-#1 over-valuation blow-up (pre-fix NVDA was ~4.2x its price).
# The lower bound only flags a total collapse: a cash-flow DCF on reported
# fundamentals can sit far below a market price that embeds growth those
# fundamentals do not show — Tesla closed 2026-10-30 at $399.81, 370x its
# FY2025 diluted EPS of $1.08, and the model values it at 0.033x that price
# (measured 2026-11-04, with its 9.0% CapEx fading to 5.3% D&A). The price-independent EV/EBITDA band below guards collapse in the
# model's own terms.
_RATIO_LOW = 0.02
_RATIO_HIGH = 3.0
# Implied EV / reported EBITDA. Below 3x a valuation has collapsed; above 40x
# it has blown up. Measured 2026-11-04, with CapEx fading from reported to the steady
# state: 3.9x (TSLA) to 23.4x (NVDA).
_EV_EBITDA_LOW = 3.0
_EV_EBITDA_HIGH = 40.0


@pytest.fixture()
def demo_data_module(monkeypatch):
    """demo_data reloaded with FINSIGHT_DEMO=1 so it serves offline fixtures."""
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    import finsight.api.demo_data as demo_data

    importlib.reload(demo_data)
    return demo_data


@pytest.fixture()
def demo_client(monkeypatch):
    """TestClient with FINSIGHT_DEMO=1 forced on (offline, real engine)."""
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    return TestClient(app_module.app)


# ---------------------------------------------------------------------------
# Bug #1 — DCF growth clamp / no trillion-dollar valuations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", DEMO_TICKERS)
def test_intrinsic_value_positive_and_sane(demo_data_module, ticker):
    """Every demo ticker's DCF per-share value is positive and within a sane
    band of its fixture current price (locks bug #1)."""
    dcf = demo_data_module.demo_dcf(ticker)
    price = demo_data_module.demo_meta(ticker)["current_price"]

    ps = dcf["intrinsic_value_per_share"]
    assert ps > 0, f"{ticker}: intrinsic value per share must be positive, got {ps}"

    ratio = ps / price
    assert _RATIO_LOW <= ratio <= _RATIO_HIGH, (
        f"{ticker}: intrinsic ${ps:,.2f} vs fixture price ${price:,.2f} "
        f"= {ratio:.2f}x — outside sane band [{_RATIO_LOW}x, {_RATIO_HIGH}x]. "
        "A growth-compounding regression (bug #1) would blow past the upper bound."
    )


@pytest.mark.parametrize("ticker", DEMO_TICKERS)
def test_growth_schedule_never_exceeds_cap(demo_data_module, ticker):
    """Inferred year-1 growth is clamped to <=40% and moves linearly to the
    terminal rate; no projected growth rate may exceed the cap (root cause of
    bug #1)."""
    from finsight.models.dcf import DCFModel

    kpis = demo_data_module._kpis_for(ticker)
    model = DCFModel()
    a = model._infer_assumptions(kpis)
    assert all(g <= DCFModel.MAX_YEAR1_GROWTH + 1e-9 for g in a.revenue_growth_rates), (
        f"{ticker}: growth schedule {a.revenue_growth_rates} exceeds "
        f"{DCFModel.MAX_YEAR1_GROWTH:.0%} cap"
    )
    # Growth moves monotonically from year 1 to the terminal rate: down for a
    # fast grower, UP for a shrinking company (flooring year-1 growth at the terminal
    # rate would model decliners as growing).
    rates = a.revenue_growth_rates
    step = 1 if rates[-1] >= rates[0] else -1
    for i in range(len(rates) - 1):
        assert step * (rates[i + 1] - rates[i]) >= -1e-9
    assert rates[-1] == pytest.approx(a.terminal_growth_rate)


@pytest.mark.parametrize("ticker", DEMO_TICKERS)
def test_implied_ev_to_ebitda_is_sane(demo_data_module, ticker):
    """The model's EV over the company's own reported EBITDA stays in a sane
    band — independent of where the market price happens to be."""
    dcf = demo_data_module.demo_dcf(ticker)
    ebitda = demo_data_module.demo_kpis(ticker)["ebitda"]
    multiple = dcf["enterprise_value_m"] / ebitda
    assert _EV_EBITDA_LOW <= multiple <= _EV_EBITDA_HIGH, (
        f"{ticker}: implied EV/EBITDA {multiple:.1f}x outside "
        f"[{_EV_EBITDA_LOW}x, {_EV_EBITDA_HIGH}x]"
    )


def test_nvda_enterprise_value_not_absurd(demo_data_module):
    """NVDA's enterprise value must be sane: a 126% trailing growth figure compounding unchecked would give ~$12.3T."""
    dcf = demo_data_module.demo_dcf("NVDA")
    ev_m = dcf["enterprise_value_m"]  # in $M
    ten_trillion_m = 10_000_000  # $10T expressed in $M
    assert 0 < ev_m < ten_trillion_m, (
        f"NVDA enterprise value ${ev_m:,.0f}M (${ev_m/1e6:.2f}T) is not sane; "
        "expected well under $10T (bug #1 regression)."
    )


# ---------------------------------------------------------------------------
# Bug #3 — /dcf wacc & terminal_growth params are actually applied
# ---------------------------------------------------------------------------

def test_dcf_wacc_param_lowers_valuation(demo_client):
    """A higher WACC must yield a strictly lower intrinsic value — proves the
    ``wacc`` query param is threaded into the model, not ignored (bug #3)."""
    low = demo_client.get("/dcf/AAPL?wacc=0.08").json()
    high = demo_client.get("/dcf/AAPL?wacc=0.15").json()

    # Params are honored (echoed back accurately).
    assert low["assumptions"]["wacc"] == pytest.approx(0.08)
    assert high["assumptions"]["wacc"] == pytest.approx(0.15)

    # And they actually change the answer in the correct direction.
    assert high["intrinsic_value_per_share"] < low["intrinsic_value_per_share"], (
        "wacc=0.15 should produce a LOWER intrinsic value than wacc=0.08; "
        f"got high={high['intrinsic_value_per_share']:.2f} "
        f"low={low['intrinsic_value_per_share']:.2f} (bug #3 regression)."
    )


def test_dcf_terminal_growth_param_raises_valuation(demo_client):
    """A higher terminal growth rate must yield a higher intrinsic value —
    proves the ``terminal_growth`` param is applied (bug #3)."""
    low_tg = demo_client.get("/dcf/AAPL?terminal_growth=0.02").json()
    high_tg = demo_client.get("/dcf/AAPL?terminal_growth=0.04").json()

    assert low_tg["assumptions"]["terminal_growth_rate"] == pytest.approx(0.02)
    assert high_tg["assumptions"]["terminal_growth_rate"] == pytest.approx(0.04)

    assert (
        high_tg["intrinsic_value_per_share"] > low_tg["intrinsic_value_per_share"]
    ), "Higher terminal growth should raise intrinsic value (bug #3 regression)."


# ---------------------------------------------------------------------------
# CapEx: year 1 is what the company actually invests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", DEMO_TICKERS)
def test_year1_capex_is_reported_and_the_fade_is_disclosed(demo_data_module, ticker):
    """All five demo tickers report CapEx above the steady state: each is charged its reported CapEx in year 1."""
    dcf = demo_data_module.demo_dcf(ticker)
    k = demo_data_module.demo_kpis(ticker)
    path = dcf["assumptions"]["capex_pct_schedule"]
    assert path[0] == pytest.approx(k["capex"] / k["revenue"])
    amortization = (k.get("amortization_of_intangibles") or 0) / k["revenue"]
    assert path[-1] == pytest.approx(dcf["assumptions"]["da_as_pct_revenue"] - amortization)
    assert dcf["assumptions"]["terminal_capex_as_pct_revenue"] == pytest.approx(path[-1])
    assert "capex_as_pct_revenue" in dcf["adjustments"]


@pytest.mark.parametrize("ticker,flat", [("MSFT", "$120.93"), ("GOOGL", "$47.54")])
def test_the_documented_value_with_capex_flat_in_perpetuity(ticker, flat):
    """README's bound on what the terminal CapEx assumption moves."""
    import dataclasses
    from pathlib import Path

    from finsight.data import companyfacts_snapshot
    from finsight.extraction.kpi_extractor import KPIExtractor
    from finsight.models.dcf import DCFModel

    a = DCFModel().value_from_kpis(
        KPIExtractor().extract_from_companyfacts(companyfacts_snapshot(ticker), ticker)).assumptions
    held = DCFModel().run(dataclasses.replace(a, terminal_capex_as_pct_revenue=a.capex_as_pct_revenue),
                          compute_sensitivity=False)
    assert f"${held.intrinsic_value_per_share:,.2f}" == flat
    assert flat in (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


def test_the_documented_worth_of_the_amortization_rule():
    """Apple's FY2026 value with CapEx fading to total D&A, as the README states it."""
    from pathlib import Path

    from tests.hand_derivation import APPLE_FY2026, hand_dcf

    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8").split())
    total_da = hand_dcf(**{**APPLE_FY2026, "amortization": 0})
    assert f"${total_da:.2f}" == "$159.81" and "$159.81" in readme
    assert f"${hand_dcf(**APPLE_FY2026) - total_da:.2f}" == "$5.50" and "$5.50 of the $165.31" in readme


@pytest.mark.parametrize("ticker,inputs,capped", [
    ("MSFT", MICROSOFT_FY2026, 343.16),
    ("GOOGL", ALPHABET_FY2025, 157.77),
])
def test_heavy_investors_are_valued_on_their_reported_capex(demo_data_module, ticker, inputs, capped):
    """Swapping reported CapEx for the steady state from year 1 would value MSFT at $343.16
    and GOOGL at $157.77, projecting more year-1 FCF than either generated. The value
    matches the independent derivation of the fade method, and year-1 FCF stays below the
    reported FCF scaled by revenue growth."""
    dcf = demo_data_module.demo_dcf(ticker)
    k = demo_data_module.demo_kpis(ticker)
    assert dcf["intrinsic_value_per_share"] == pytest.approx(hand_dcf(**inputs), rel=1e-12)
    assert dcf["intrinsic_value_per_share"] < capped - 10
    scaled_reported_fcf = k["free_cash_flow"] * dcf["projected_revenues_m"][0] / k["revenue"]
    assert dcf["projected_fcfs_m"][0] < scaled_reported_fcf

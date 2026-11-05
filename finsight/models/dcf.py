"""
Automated Discounted Cash Flow (DCF) valuation model.

Builds a 5-10 year FCF projection based on extracted KPIs and historical
financials, then discounts to present value using WACC.

Valuation methodology:
  FCF_t = EBIT_t × (1 − tax) + D&A_t − CapEx_t − ΔNWC_t
  ΔNWC_t = nwc_pct_of_revenue_change × (Revenue_t − Revenue_{t−1})
  CapEx_t / Revenue_t starts at ``capex_as_pct_revenue`` in year 1 and, when
    ``terminal_capex_as_pct_revenue`` is set, moves linearly to it by year n;
    the terminal year uses the steady-state rate. Inferred from KPIs, year 1
    is the REPORTED intensity and the steady state is the D&A rate.
  Terminal value (Gordon growth) = FCF_{n+1} / (WACC − g), where FCF_{n+1} is
    a normalised year: revenue grows at g, so its ΔNWC reflects g too
  Enterprise Value = PV(projected FCFs) + PV(terminal value)
  Equity Value = Enterprise Value − Net Debt, where net debt (inferred from
    KPIs) is total debt − cash and cash equivalents; marketable securities
    (current and non-current) are NOT netted, and noncontrolling interests and
    preferred stock are NOT deducted
  Intrinsic Value per Share = Equity Value / shares — inferred from KPIs, the
    fiscal year's diluted weighted-average count (period-end basic only when
    no diluted count is reported; the source says which)
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DCFAssumptions:
    """Input assumptions for the DCF model."""

    # Growth assumptions
    revenue_growth_rates: list[float] = field(default_factory=lambda: [0.15, 0.12, 0.10, 0.08, 0.07])
    """List of YoY revenue growth rates for projection years. len = n_years."""

    terminal_growth_rate: float = 0.03
    """Long-term perpetuity growth rate (default 3% = GDP-level)."""

    # Margin assumptions
    ebitda_margin: float = 0.20
    """Target EBITDA margin in projection years."""

    da_as_pct_revenue: float = 0.05
    """D&A as % of revenue (for FCF from EBITDA)."""

    capex_as_pct_revenue: float = 0.05
    """CapEx as % of revenue in projection year 1."""

    terminal_capex_as_pct_revenue: float | None = None
    """Steady-state CapEx as % of revenue, reached linearly by the final
    projection year and used for the terminal year. ``None`` holds
    ``capex_as_pct_revenue`` flat throughout. :meth:`DCFModel.value_from_kpis`
    sets it to the D&A rate: a perpetuity cannot out-invest depreciation
    forever, but a company investing heavily today does so in year 1."""

    nwc_pct_of_revenue_change: float = 0.02
    """Net working capital invested per dollar of revenue CHANGE:
    ΔNWC_t = nwc_pct_of_revenue_change × (Revenue_t − Revenue_{t−1}).

    Equivalent to holding NWC at a constant share of revenue: working capital
    is invested only as revenue grows, and released as it shrinks. (Charging
    a percentage of the revenue LEVEL every year instead implies NWC climbing
    towards pct × (1 + g) / g of revenue — about 69% at 2% and 3% growth.)"""

    tax_rate: float = 0.21
    """Effective tax rate."""

    # Discount rate
    wacc: float = 0.10
    """Weighted Average Cost of Capital."""

    # Balance sheet
    net_debt: float = 0.0
    """Total debt minus cash and cash equivalents, in $M (marketable
    securities not netted when inferred from KPIs)."""

    shares_outstanding: float = 1_000.0
    """Share count for the per-share value, in millions (inferred: the
    diluted weighted-average count for the fiscal year)."""

    # Metadata
    base_revenue: float = 1_000.0
    """Most recent fiscal year revenue, in $M."""

    sources: dict[str, str] = field(default_factory=dict)
    """Where each inferred assumption came from (reported figure, caller
    override, or ``"default: …"``). Filled by :meth:`DCFModel.value_from_kpis`;
    empty for hand-built assumptions."""

    adjustments: dict[str, str] = field(default_factory=dict)
    """Every input the projection moves away from the reported figure it
    starts from — a clamp, a fade toward a steady state, an unreported side
    counted as zero — with what was done. Filled by
    :meth:`DCFModel.value_from_kpis`; empty for hand-built assumptions."""

    @property
    def assumed_fundamentals(self) -> list[str]:
        """Company figures the filing did not report and the model ASSUMED
        (D&A, CapEx, net debt, year-1 growth) — as opposed to the tunable
        model parameters in :data:`MODEL_PARAMETERS`. Every response that
        shows a value built on one names it next to the value."""
        return sorted(k for k, v in self.sources.items()
                      if v.startswith("default") and k not in MODEL_PARAMETERS)

    growth_fades_to_terminal: bool = False
    """True when the growth schedule moves linearly from year 1 to
    ``terminal_growth_rate`` (as :meth:`DCFModel.value_from_kpis` builds it):
    the sensitivity table then re-fades it to each cell's terminal rate, so a
    cell equals ``value_from_kpis(terminal_growth=…)`` for that rate."""

    limitations: dict[str, str] = field(default_factory=dict)
    """What the reported inputs cannot show — e.g. D&A that is property and
    equipment depreciation only, or finance leases outside CapEx and debt —
    carried over from the KPI provenance. Disclosed, not corrected for."""


# Inputs that are modelling choices, not company figures: defaulted and
# disclosed, and the caller can set the first two (/dcf ?wacc= &terminal_growth=).
MODEL_PARAMETERS = frozenset({"wacc", "terminal_growth_rate",
                              "nwc_pct_of_revenue_change", "tax_rate"})

MIN_EBITDA_MARGIN = 0.05
"""Below this base EBITDA margin the DCF refuses (:class:`DCFInputError`)."""


def _signed_m(value: float) -> str:
    """$M with the sign before the dollar: "−$57M", not "$-57M"."""
    return f"{'−' if value < 0 else ''}${abs(value):,.0f}M"


class DCFInputError(ValueError):
    """A required valuation input is missing. The model refuses to value the
    company rather than substitute a placeholder for a fundamental."""


@dataclass
class DCFResult:
    """Output from the DCF valuation model."""

    intrinsic_value_per_share: float
    enterprise_value: float
    equity_value: float
    terminal_value: float
    pv_terminal_value: float
    pv_fcfs: float
    projected_fcfs: list[float]
    projected_revenues: list[float]
    assumptions: DCFAssumptions
    sensitivity_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    projected_capex_pct: list[float] = field(default_factory=list)
    """CapEx as % of revenue used in each projection year."""

    @property
    def terminal_value_pct(self) -> float:
        if self.enterprise_value > 0:
            return self.pv_terminal_value / self.enterprise_value
        return 0.0

    def __repr__(self) -> str:
        return (
            f"DCFResult("
            f"intrinsic=${self.intrinsic_value_per_share:.2f}, "
            f"EV=${self.enterprise_value/1e3:.1f}B, "
            f"TV%={self.terminal_value_pct:.1%})"
        )


class DCFModel:
    """
    Automated 5-year DCF valuation model.

    Parameters
    ----------
    n_years : int
        Number of projection years (default 5).

    Examples
    --------
    Runnable (``tests/test_dcf.py`` doctests it):

    >>> from finsight.extraction.kpi_extractor import ExtractedKPIs
    >>> kpis = ExtractedKPIs(ticker="X", period="FY2025", source_type="10-K",
    ...                      revenue=10_000, operating_income=2_000, ebitda=2_500,
    ...                      capex=400, shares_outstanding=1_000, revenue_growth_yoy=0.05)
    >>> result = DCFModel().value_from_kpis(kpis)
    >>> round(result.intrinsic_value_per_share, 2)
    24.45
    >>> result.assumptions.assumed_fundamentals     # no debt or cash reported
    ['net_debt']
    """

    def __init__(self, n_years: int = 5) -> None:
        self.n_years = n_years

    def value_from_kpis(
        self,
        kpis: Any,
        assumptions: DCFAssumptions | None = None,
        wacc: float | None = None,
        terminal_growth: float | None = None,
    ) -> DCFResult:
        """
        Build DCF from extracted KPIs. Infers assumptions from historical data.

        Parameters
        ----------
        kpis : ExtractedKPIs
            Extracted financial metrics.
        assumptions : DCFAssumptions | None
            Override assumptions. If None, infers from KPIs.
        wacc : float | None
            Override the discount rate. Only applied when ``assumptions`` is
            None (i.e. when inferring from KPIs). Threads the caller's WACC
            (e.g. a ``/dcf`` query param) into the inferred assumptions so it
            is actually used rather than silently defaulted.
        terminal_growth : float | None
            Override the terminal (perpetuity) growth rate. Same semantics as
            ``wacc``.

        Returns
        -------
        DCFResult
        """
        inferred = assumptions is None
        if inferred:
            assumptions = self._infer_assumptions(
                kpis, wacc=wacc, terminal_growth=terminal_growth
            )
        result = self.run(assumptions)
        # A non-positive equity value is not a valuation (net debt exceeds the
        # enterprise value): refuse rather than print a negative price.
        if result.equity_value <= 0:
            raise DCFInputError(
                f"Not meaningful: the DCF equity value is "
                f"${result.equity_value:,.0f}M (enterprise value "
                f"${result.enterprise_value:,.0f}M less net debt "
                f"${assumptions.net_debt:,.0f}M), so there is no positive "
                "per-share value to report."
            )
        # Net cash can leave a positive equity value on a business the model values at
        # less than nothing: a per-share value that is all cash, which is no valuation.
        if result.enterprise_value <= 0:
            raise DCFInputError(
                f"Not meaningful: the DCF enterprise value is "
                f"${result.enterprise_value:,.0f}M, so the model puts no positive value on "
                f"the operations. The ${result.equity_value:,.0f}M equity value would be "
                "nothing but net cash less that, not a valuation of the business."
            )
        if inferred:
            self._disclose_period_end_shares(kpis, result)
        return result

    @staticmethod
    def _disclose_period_end_shares(kpis: Any, result: DCFResult) -> None:
        """Say what the year-end count would move when it is above the diluted count
        divided by. Disclosed, not corrected for: Tesla's 3,528M against 3,751M moves its
        value by more than its current debt does."""
        provenance = getattr(kpis, "provenance", None) or {}
        period_end = provenance.get("period_end_shares_above_diluted")
        if not period_end:
            return
        used = result.assumptions.shares_outstanding
        value = result.intrinsic_value_per_share
        other = result.equity_value / period_end
        result.assumptions.limitations["share_count"] = (
            f"The value divides by the {used:,.0f}M diluted weighted-average count for "
            f"{kpis.period}, which is below the {period_end:,.0f}M shares outstanding at that "
            f"fiscal year-end (CommonStockSharesOutstanding, same 10-K). On {period_end:,.0f}M, "
            f"the ${value:,.2f} value would be ${other:,.2f}: ${value - other:,.2f}, or "
            f"{(value - other) / value:.1%}, lower. The value shown stays on the diluted count; "
            "this says how far the choice moves it, not which count is right."
        )

    def run(
        self,
        assumptions: DCFAssumptions,
        compute_sensitivity: bool = True,
    ) -> DCFResult:
        """
        Run the DCF model with explicit assumptions.

        Parameters
        ----------
        assumptions : DCFAssumptions
            Full set of model inputs.
        compute_sensitivity : bool
            When False, skip the WACC × terminal growth sensitivity table.
            Used internally by :meth:`_sensitivity_table` to avoid recursion.

        Returns
        -------
        DCFResult
        """
        a = assumptions
        self.check_spread(a.wacc, a.terminal_growth_rate)
        # The horizon is n_years, and every projected year needs its growth rate.
        n = self.n_years
        if len(a.revenue_growth_rates) < n:
            raise ValueError(
                f"A {n}-year projection needs {n} revenue growth rates; the "
                f"assumptions give {len(a.revenue_growth_rates)}. Pass one per "
                "year (value_from_kpis builds them) or use a shorter n_years."
            )
        # Per-share value needs a real share count.
        if not a.shares_outstanding or a.shares_outstanding <= 0:
            raise ValueError(
                f"shares_outstanding must be positive (got {a.shares_outstanding!r}); "
                "a per-share value needs a share count."
            )
        growth_rates = list(a.revenue_growth_rates[:n])

        # Project revenues and FCFs. ΔNWC depends on the revenue change, so
        # each year's FCF needs the prior year's revenue.
        capex_path = self.capex_schedule(a, n)
        revenues: list[float] = []
        fcfs: list[float] = []
        prior = a.base_revenue
        for i in range(n):
            rev = prior * (1 + growth_rates[i])
            revenues.append(rev)
            fcfs.append(self._fcf(rev, prior, a, capex_path[i]))
            prior = rev

        # Discount FCFs
        pv_fcfs_list: list[float] = []
        for i, fcf in enumerate(fcfs):
            pv = fcf / (1 + a.wacc) ** (i + 1)
            pv_fcfs_list.append(pv)
        total_pv_fcfs = sum(pv_fcfs_list)

        # Terminal value (Gordon growth) on a normalised year n+1 whose revenue
        # grows at the terminal rate — so the perpetuity carries terminal-rate
        # working-capital investment, not year n's — and whose CapEx is at the
        # steady-state rate.
        terminal_revenue = revenues[-1] * (1 + a.terminal_growth_rate)
        terminal_fcf = self._fcf(
            terminal_revenue, revenues[-1], a, self._steady_state_capex(a)
        )
        terminal_value = terminal_fcf / (a.wacc - a.terminal_growth_rate)
        pv_terminal_value = terminal_value / (1 + a.wacc) ** n

        # Enterprise and equity value
        enterprise_value = total_pv_fcfs + pv_terminal_value
        equity_value = enterprise_value - a.net_debt
        intrinsic_per_share = equity_value / a.shares_outstanding

        # Sensitivity analysis (WACC × terminal growth)
        sensitivity = (
            self._sensitivity_table(assumptions, fcfs)
            if compute_sensitivity
            else pd.DataFrame()
        )

        return DCFResult(
            intrinsic_value_per_share=intrinsic_per_share,
            enterprise_value=enterprise_value,
            equity_value=equity_value,
            terminal_value=terminal_value,
            pv_terminal_value=pv_terminal_value,
            pv_fcfs=total_pv_fcfs,
            projected_fcfs=fcfs,
            projected_revenues=revenues,
            assumptions=assumptions,
            sensitivity_table=sensitivity,
            projected_capex_pct=capex_path,
        )

    @staticmethod
    def _steady_state_capex(a: DCFAssumptions) -> float:
        if a.terminal_capex_as_pct_revenue is None:
            return a.capex_as_pct_revenue
        return a.terminal_capex_as_pct_revenue

    @classmethod
    def capex_schedule(cls, a: DCFAssumptions, n: int) -> list[float]:
        """CapEx as % of revenue for projection years 1..n.

        Year 1 is ``capex_as_pct_revenue``; with a steady-state rate set, each
        later year moves an equal step toward it, arriving in year ``n``.
        """
        start, end = a.capex_as_pct_revenue, cls._steady_state_capex(a)
        if n <= 1:
            return [start] * n
        return [start + (end - start) * i / (n - 1) for i in range(n)]

    @staticmethod
    def _fcf(revenue: float, prior_revenue: float, a: DCFAssumptions,
             capex_pct: float) -> float:
        """Unlevered free cash flow for one year at the given CapEx intensity."""
        ebitda = revenue * a.ebitda_margin
        da = revenue * a.da_as_pct_revenue
        ebit = ebitda - da
        # Tax is never negative: a loss earns no perpetual tax credit.
        nopat = ebit - a.tax_rate * max(ebit, 0.0)
        capex = revenue * capex_pct
        delta_nwc = a.nwc_pct_of_revenue_change * (revenue - prior_revenue)
        return nopat + da - capex - delta_nwc

    # The Gordon terminal multiple is 1 / (WACC − g): it explodes as the spread
    # narrows (0.1 pp → 1,000x terminal FCF) and is undefined at zero. Below
    # this spread the model refuses to answer rather than print an artefact.
    MIN_WACC_SPREAD = 0.02

    @classmethod
    def spread_ok(cls, wacc: float, terminal_growth: float) -> bool:
        # The epsilon keeps e.g. 0.07 − 0.05 (= 0.01999…98 in floating point)
        # on the right side of the line.
        return wacc - terminal_growth >= cls.MIN_WACC_SPREAD - 1e-9

    @classmethod
    def check_spread(cls, wacc: float, terminal_growth: float) -> None:
        """Raise ``ValueError`` unless WACC exceeds g by ``MIN_WACC_SPREAD``."""
        if not cls.spread_ok(wacc, terminal_growth):
            raise ValueError(
                f"WACC ({wacc:.2%}) must exceed the terminal growth rate "
                f"({terminal_growth:.2%}) by at least {cls.MIN_WACC_SPREAD:.0%}: "
                "the terminal value is FCF / (WACC − g), which is undefined at a "
                "zero spread and meaningless near one."
            )

    def _sensitivity_table(
        self,
        assumptions: DCFAssumptions,
        fcfs: list[float],
    ) -> pd.DataFrame:
        """
        2D sensitivity: intrinsic value per share vs. WACC and terminal growth rate.
        """
        wacc_range = [assumptions.wacc + delta for delta in [-0.02, -0.01, 0, 0.01, 0.02]]
        tgr_range = [assumptions.terminal_growth_rate + delta
                     for delta in [-0.01, 0, 0.01, 0.02]]

        rows = {}
        for wacc in wacc_range:
            row = {}
            for tgr in tgr_range:
                if not self.spread_ok(wacc, tgr):
                    row[f"TGR={tgr:.1%}"] = float("nan")
                    continue
                # Recompute with new assumptions (skip nested sensitivity)
                rates = list(assumptions.revenue_growth_rates)
                if assumptions.growth_fades_to_terminal and len(rates) > 1:
                    # The schedule fades to the terminal rate; re-fade it to THIS
                    # cell's rate, so each cell is what the endpoint returns for it.
                    g1, n = rates[0], len(rates)
                    rates = [g1 + (tgr - g1) * (i / (n - 1)) for i in range(n)]
                temp = DCFAssumptions(**{**assumptions.__dict__,
                                        "wacc": wacc,
                                        "terminal_growth_rate": tgr,
                                        "revenue_growth_rates": rates})
                r = self.run(temp, compute_sensitivity=False)
                row[f"TGR={tgr:.1%}"] = round(r.intrinsic_value_per_share, 2)
            rows[f"WACC={wacc:.1%}"] = row

        return pd.DataFrame(rows).T

    # Band for the year-1 revenue growth fed into a projection. A trailing YoY
    # figure like NVDA's +126% is a one-off inflection, not a sustainable
    # multi-year rate; compounding it produces a nonsensical FCF stream (and a
    # multi-trillion-dollar enterprise value). Symmetrically, a one-year
    # collapse is not a five-year trend. Growth then moves linearly to the
    # terminal rate, so the band only shapes the early years.
    MAX_YEAR1_GROWTH = 0.40
    MIN_YEAR1_GROWTH = -0.30

    def _infer_assumptions(
        self,
        kpis: Any,
        wacc: float | None = None,
        terminal_growth: float | None = None,
    ) -> DCFAssumptions:
        """Infer DCF assumptions from extracted KPIs.

        Revenue and diluted shares are REQUIRED: without them there is no
        company-specific valuation, so :class:`DCFInputError` is raised instead
        of substituting a placeholder. Every other input is inferred from the
        KPIs where reported and otherwise falls back to a model default; either
        way the choice is recorded in ``DCFAssumptions.sources`` (defaults are
        prefixed ``"default:"``), so a response can say exactly what was assumed.

        Year-1 revenue growth comes from forward guidance when present, else
        the trailing YoY figure; it is clamped to
        [``MIN_YEAR1_GROWTH``, ``MAX_YEAR1_GROWTH``] and then moves **linearly**
        to the terminal rate across the horizon — a shrinking company is
        modelled as shrinking, then recovering to terminal growth.

        CapEx follows the same shape: year 1 at the **reported** CapEx /
        revenue, moving linearly to the D&A rate by year ``n``, which the
        terminal year keeps (steady state: CapEx = D&A, i.e. no net
        investment in perpetuity — a simplification that is generous to a
        company still growing at the terminal rate). Every such departure from
        a reported figure is described in ``DCFAssumptions.adjustments``.

        Parameters
        ----------
        wacc, terminal_growth : float | None
            Optional overrides threaded from the caller (e.g. ``/dcf`` query
            params). When provided they replace the model defaults so the
            echoed assumptions match what was actually used.
        """
        period = getattr(kpis, "period", "") or "the extracted period"
        missing = [
            name for name, value in (
                ("revenue", kpis.revenue),
                ("shares_outstanding", kpis.shares_outstanding),
            )
            if value is None or value <= 0
        ]
        if missing:
            raise DCFInputError(
                f"Cannot value {getattr(kpis, 'ticker', 'this company')}: "
                f"{' and '.join(missing)} not reported for {period}. FinSight "
                "does not substitute placeholder fundamentals."
            )

        sources: dict[str, str] = {}
        adjustments: dict[str, str] = {}
        n = self.n_years
        revenue = float(kpis.revenue)
        sources["base_revenue"] = f"reported revenue, {period}"
        concepts = (getattr(kpis, "provenance", None) or {}).get("concepts", {})
        share_concept = concepts.get("shares_outstanding")
        if share_concept == "CommonStockSharesOutstanding":
            sources["shares_outstanding"] = (
                f"reported period-end basic shares outstanding, {period} — no diluted "
                "count reported, so dilution from options/RSUs is not included"
            )
        else:
            sources["shares_outstanding"] = (
                f"reported diluted weighted-average shares over {period} "
                f"({share_concept or 'diluted count'}), not the period-end count"
            )
        basis = (getattr(kpis, "provenance", None) or {}).get("per_share_basis")
        if basis:
            sources["shares_outstanding"] += f" — {basis}"

        if terminal_growth is not None:
            terminal_g = float(terminal_growth)
            sources["terminal_growth_rate"] = "caller"
        else:
            terminal_g = DCFAssumptions.terminal_growth_rate
            sources["terminal_growth_rate"] = f"default: {terminal_g:.1%}"
        if wacc is not None:
            discount = float(wacc)
            sources["wacc"] = "caller"
        else:
            discount = DCFAssumptions.wacc
            sources["wacc"] = f"default: {discount:.1%}"

        # Year-1 growth: forward guidance midpoint, else trailing YoY, else
        # flat at the terminal rate (no growth the data does not show).
        g_low = getattr(kpis, "guidance_revenue_low", None)
        g_high = getattr(kpis, "guidance_revenue_high", None)
        guidance_note = ""
        if (g_low or g_high) and getattr(kpis, "guidance_period", None) != "annual":
            # Only FULL-YEAR guidance is comparable with annual revenue: a
            # quarter's guidance read as −74% growth (AAPL $106.64 → $39.80).
            guidance_note = "; revenue guidance ignored (not stated as full-year)"
            g_low = g_high = None
        if g_low or g_high:
            guide_mid = ((g_low or g_high) + (g_high or g_low)) / 2.0
            g1 = (guide_mid - revenue) / revenue
            g1_source = "full-year revenue guidance midpoint"
        elif kpis.revenue_growth_yoy is not None:
            g1 = float(kpis.revenue_growth_yoy)
            g1_source = f"trailing revenue growth, {period}"
        else:
            g1 = terminal_g
            g1_source = "default: no prior-year revenue, so flat at terminal growth"
        clamped = min(max(g1, self.MIN_YEAR1_GROWTH), self.MAX_YEAR1_GROWTH)
        if clamped != g1:
            g1_source += f" ({g1:.1%}, clamped to {clamped:.1%})"
        if not g1_source.startswith("default"):
            adjustments["revenue_growth_rates"] = (
                f"year 1 at {g1_source.split(' (')[0]} ({g1:.1%}"
                + (f", clamped to {clamped:.1%}" if clamped != g1 else "")
                + f"), moving linearly to the {terminal_g:.1%} terminal rate "
                f"by year {n}"
            )
        g1 = clamped
        sources["revenue_growth_rates"] = (
            f"{g1_source}; linear to terminal growth by year {n}{guidance_note}"
        )

        if n <= 1:
            growth_schedule = [g1]
        else:
            growth_schedule = [
                g1 + (terminal_g - g1) * (i / (n - 1))
                for i in range(n)
            ]

        # D&A intensity: reported (EBITDA − operating income) / revenue.
        da_pct = DCFAssumptions.da_as_pct_revenue
        sources["da_as_pct_revenue"] = f"default: {da_pct:.0%} of revenue"
        if kpis.ebitda is not None and kpis.operating_income is not None:
            reported_da = (kpis.ebitda - kpis.operating_income) / revenue
            if reported_da >= 0:
                da_pct = reported_da
                da_from = re.sub(r"^OperatingIncomeLoss \+ | \(derived\)$", "",
                                 concepts.get("ebitda", ""))
                sources["da_as_pct_revenue"] = (
                    f"reported (EBITDA − operating income) / revenue, {period}"
                    + (f" — D&A from {da_from}" if da_from else ""))

        # Steady-state reinvestment: the rate CapEx fades to and the terminal
        # year keeps. Total D&A is the right non-cash ADD-BACK, but CapEx only
        # replaces property and equipment: amortization of ACQUIRED intangibles
        # is non-cash and is not replaced by CapEx (replacing it would mean new
        # acquisitions, which are outside this model). Fading to total D&A would force a
        # serial acquirer's CapEx from 1.1% to 20.4% of revenue and roughly halve its value.
        reinvest_pct = da_pct
        reinvest_source = f"the D&A rate ({da_pct:.1%})"
        limitations: dict[str, str] = dict(
            (getattr(kpis, "provenance", None) or {}).get("limitations", {}))
        amort = getattr(kpis, "amortization_of_intangibles", None)
        dep = getattr(kpis, "depreciation", None)
        reported_da_m = da_pct * revenue if sources["da_as_pct_revenue"].startswith("reported") else None
        if reported_da_m is not None and amort is not None and 0 < amort < reported_da_m:
            reinvest_pct = (reported_da_m - amort) / revenue
            reinvest_source = (
                f"D&A less amortization of acquired intangibles "
                f"(AmortizationOfIntangibleAssets) ({reinvest_pct:.1%})")
            limitations["steady_state_capex"] = (
                f"Steady-state CapEx is D&A less amortization of acquired intangibles: "
                f"{reinvest_pct:.1%} of revenue, not the {da_pct:.1%} D&A rate. The "
                f"{amort / revenue:.1%} of amortization is added back as non-cash and is "
                "not treated as reinvestment; future acquisitions (and the amortization "
                "running off) are outside the model."
            )
        elif reported_da_m is not None and "Depreciation" != re.sub(
                r"^OperatingIncomeLoss \+ | \(derived\)$", "", concepts.get("ebitda", "")):
            dep_note = (f" Of it, depreciation of property and equipment is "
                        f"{dep / revenue:.1%} of revenue." if dep else "")
            unknown = "AmortizationOfIntangibleAssets" in (
                (getattr(kpis, "provenance", None) or {}).get("not_in_snapshot") or [])
            why = ("the bundled snapshot does not carry AmortizationOfIntangibleAssets "
                   "(it was cut before FinSight read it), so whether any is reported "
                   "separately is unknown until a refresh"
                   if unknown else
                   "no amortization of acquired intangibles is reported separately")
            limitations["steady_state_capex"] = (
                f"Steady-state CapEx is the combined D&A rate ({da_pct:.1%}): {why}. "
                "All of D&A is therefore treated as needing replacement — conservative, "
                f"since any acquisition amortization inside it lowers the value.{dep_note}"
            )

        # An operating loss is not modelled: the margin is held flat into
        # perpetuity, so a non-positive EBIT would be a perpetual loss. The EBITDA check
        # below cannot catch this case: −1.5% EBIT with an 18% EBITDA margin passes it.
        if kpis.operating_income is not None and kpis.operating_income <= 0:
            raise DCFInputError(
                f"Not meaningful: operating income is "
                f"${kpis.operating_income:,.0f}M ({kpis.operating_income / revenue:.1%} of "
                f"revenue) for {period}. This model holds the margin flat into "
                "perpetuity, so it cannot value a company with an operating loss."
            )

        # EBITDA margin: reported EBITDA, else operating margin + D&A.
        if kpis.ebitda is not None:
            margin = kpis.ebitda / revenue
            margin_source = f"reported EBITDA / revenue, {period}"
        elif kpis.operating_income is not None:
            margin = kpis.operating_income / revenue + da_pct
            margin_source = f"reported operating margin + D&A, {period}"
        else:
            # No placeholder margin: a company that omits its operating-income tag may
            # be a loss-maker, and would be valued as a profitable one.
            raise DCFInputError(
                f"Cannot value {getattr(kpis, 'ticker', 'this company')}: neither "
                f"operating income nor EBITDA is reported for {period}, so there "
                "is no margin to project. FinSight does not substitute a "
                "placeholder margin."
            )
        # Below the floor there is no meaningful DCF here: the model holds the
        # margin flat into perpetuity, so a loss-maker would be valued on a loss forever,
        # and lifting the margin to the floor would value it as roughly break-even.
        if margin < MIN_EBITDA_MARGIN:
            raise DCFInputError(
                f"EBITDA margin {margin:.1%} ({margin_source}) is below the "
                f"{MIN_EBITDA_MARGIN:.0%} this model needs: it holds the margin "
                "flat into perpetuity, so no meaningful DCF value can be produced "
                "for a company at or near a loss. No value is shown rather than "
                "one computed on a raised margin."
            )
        bounded = min(margin, 0.60)
        if bounded != margin:
            margin_source += f" ({margin:.1%}, clamped to {bounded:.1%})"
            adjustments["ebitda_margin"] = (
                f"reported {margin:.1%} clamped to {bounded:.1%} "
                "(the model caps the margin at 60%; below 5% it refuses)"
            )
        sources["ebitda_margin"] = margin_source

        # CapEx intensity: year 1 at the reported rate — a company investing
        # heavily now spends that cash now — moving linearly to the D&A rate
        # by year n. The terminal year stays at D&A: a perpetuity cannot
        # out-invest depreciation forever without the asset base (and so D&A)
        # rising too. The fade is listed as an adjustment.
        if kpis.capex is not None:
            capex_pct = kpis.capex / revenue
            capex_source = f"reported CapEx / revenue, {period} ({capex_pct:.1%})"
            if capex_pct != reinvest_pct:
                capex_source += f" in year 1, linear to {reinvest_source} by year {n}"
                if reinvest_pct == da_pct:
                    adjustments["capex_as_pct_revenue"] = (
                        f"reported {capex_pct:.1%} of revenue in year 1, moving linearly "
                        f"to the {da_pct:.1%} D&A rate by year {n} and held there in "
                        "the terminal year (steady state: CapEx = D&A)"
                    )
                else:
                    adjustments["capex_as_pct_revenue"] = (
                        f"reported {capex_pct:.1%} of revenue in year 1, moving linearly "
                        f"to {reinvest_pct:.1%} (D&A less acquired-intangible "
                        f"amortization) by year {n} and held there in the terminal year"
                    )
        else:
            capex_pct = reinvest_pct
            capex_source = (
                f"default: CapEx not reported, held at {reinvest_source}, "
                "the steady-state assumption"
            )
        sources["capex_as_pct_revenue"] = capex_source

        # Net debt from whichever side is reported; an unreported side counts
        # as zero, which the source and the adjustments both say.
        debt, cash = kpis.total_debt, kpis.cash_and_equivalents
        if debt is None and cash is None:
            net_debt = 0.0
            sources["net_debt"] = "default: 0 (neither debt nor cash reported)"
        else:
            net_debt = (debt or 0.0) - (cash or 0.0)
            note = (
                f"reported total debt ({concepts.get('total_debt', 'debt')}) − cash and "
                f"cash equivalents ({concepts.get('cash_and_equivalents', 'cash')}), "
                f"{period}; marketable securities (current and non-current) are not "
                "netted, and noncontrolling interests and preferred stock are not "
                "deducted from equity value"
            )
            unreported = [side for side, v in (("debt", debt), ("cash", cash)) if v is None]
            for side in unreported:
                note += f" ({side} not reported, counted as 0)"
            if unreported:
                adjustments["net_debt"] = (
                    f"{unreported[0]} not reported for {period}, counted as 0"
                )
                # A side counted as 0 is a company figure the model assumed, so the "default"
                # puts it in assumed_fundamentals: a filer whose $20B of debt sits under a tag
                # outside the chain reads $200 a share high on 100M shares.
                note = f"default: {unreported[0]} not reported, counted as 0; {note}"
            sources["net_debt"] = note
        if debt is None:
            limitations["debt_not_reported"] = (
                f"No debt concept FinSight reads is reported for {period} (LongTermDebt and its "
                "current and noncurrent forms, DebtCurrent, CommercialPaper, ShortTermBorrowings, "
                "LongTermDebtAndCapitalLeaseObligations, NotesPayable, SeniorNotes or "
                "ConvertibleNotesPayable and their forms), so debt counts as 0. Those are common "
                "tags, not every tag: a filer with debt under any other has its equity value and "
                "per-share value overstated by all of it."
            )

        limitations["marketable_securities"] = (
            "Net debt nets cash and cash equivalents only. Marketable securities (current and "
            "non-current) are not netted, so for a filer that holds them net debt is overstated "
            f"and the per-share value understated: each $1B not netted is "
            f"${1_000 / kpis.shares_outstanding:,.2f} a share. Noncontrolling interests and "
            "preferred stock are not deducted either, which overstates equity value for a filer "
            "that has them."
        )

        sources["nwc_pct_of_revenue_change"] = (
            f"default: {DCFAssumptions.nwc_pct_of_revenue_change:.0%} of the revenue change"
        )
        sources["tax_rate"] = f"default: {DCFAssumptions.tax_rate:.0%}"

        # Two different cash flows go by one name: Tesla's FY2025 free cash flow is $6.2B
        # and its DCF Year 1 $0.7M. This says why.
        reported_fcf = getattr(kpis, "free_cash_flow", None)
        ebit = revenue * (bounded - da_pct)
        base_year_fcf = (ebit - DCFAssumptions.tax_rate * max(ebit, 0.0)
                         + revenue * (da_pct - capex_pct))
        limitations["free_cash_flow_basis"] = (
            "The projected free cash flow is not the reported one. Projected: EBIT × (1 − tax) "
            "+ D&A − CapEx − ΔNWC. It starts from operating income, so stock-based compensation "
            "stays a cost and is never added back; it charges a flat "
            f"{DCFAssumptions.tax_rate:.0%} tax on EBIT and working capital at "
            f"{DCFAssumptions.nwc_pct_of_revenue_change:.0%} of the revenue change; interest and "
            "other non-operating income are outside it. Reported (/kpis free_cash_flow): "
            "operating cash flow − CapEx"
            + (f", {_signed_m(reported_fcf)} for {period}" if reported_fcf is not None
               else f", not available for {period}")
            + ", which adds stock-based compensation back as non-cash and holds the taxes "
            "actually paid, interest and the actual working-capital movement. The projected "
            f"formula on {period}'s own figures, before growth and ΔNWC, gives "
            f"{_signed_m(base_year_fcf)}. FinSight does not read a stock-compensation concept, "
            "so nothing here splits the difference between the two."
        )

        return DCFAssumptions(
            base_revenue=revenue,
            revenue_growth_rates=growth_schedule,
            growth_fades_to_terminal=len(growth_schedule) > 1,
            terminal_growth_rate=terminal_g,
            wacc=discount,
            ebitda_margin=float(bounded),
            da_as_pct_revenue=float(da_pct),
            net_debt=float(net_debt),
            shares_outstanding=float(kpis.shares_outstanding),
            capex_as_pct_revenue=float(capex_pct),
            terminal_capex_as_pct_revenue=float(reinvest_pct),
            sources=sources,
            adjustments=adjustments,
            limitations=limitations,
        )

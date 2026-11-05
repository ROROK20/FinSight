"""
Comparable Company Analysis (Trading Comps).

Computes a valuation range for a target company by benchmarking
its trading multiples against a peer group.

Multiples computed:
  EV/Revenue, EV/EBITDA, P/E, P/S, P/B, P/FCF
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Yahoo Finance's name for the sector, and the two other spellings a data source may use.
_FINANCIAL_SECTORS = frozenset({"financial services", "financials", "financial"})


@dataclass
class CompsResult:
    """Output from comparable company analysis."""

    target_ticker: str
    target_multiples: dict[str, float]
    peer_multiples: pd.DataFrame
    peer_stats: pd.DataFrame
    implied_valuations: dict[str, dict[str, float]]
    implied_ev_m: dict[str, float]  # low, mid, high IMPLIED ENTERPRISE VALUE in $M
    # low/mid/high implied EQUITY value ($M) from the equity-basis multiples
    # (P/E, P/S). Kept separate from implied_ev_m on purpose: mixing an
    # enterprise value with an equity value in one range is dimensionally wrong.
    implied_equity_m: dict[str, float] = field(default_factory=dict)
    # Peer multiples left OUT of the peer statistics, one entry per
    # (ticker, multiple): {"ticker", "multiple", "value", "reason"}. A negative
    # EV/EBITDA or P/E means the peer lost money on that line; averaging it in
    # dragged the median down (and a median below zero implied a negative
    # value). The raw figure stays visible in ``peer_multiples``.
    excluded_multiples: list[dict[str, Any]] = field(default_factory=list)
    # Multiples NOT applied to the target, with why — e.g. EV/EBITDA when the
    # target's EBITDA is on a different basis from the peers'.
    skipped_multiples: dict[str, str] = field(default_factory=dict)
    # Symbols with no usable market data (ticker -> reason): not retrieved, or
    # priced and reported in different currencies. They contribute nothing: no
    # stand-in multiples are invented for them.
    unavailable: dict[str, str] = field(default_factory=dict)
    # Why no enterprise value is implied at all, when the target is a financial.
    ev_refused: str | None = None

    def __repr__(self) -> str:
        low = self.implied_ev_m.get("low", 0)
        mid = self.implied_ev_m.get("mid", 0)
        high = self.implied_ev_m.get("high", 0)
        return (
            f"CompsResult({self.target_ticker} | "
            f"Implied EV: ${low:,.0f}M – ${mid:,.0f}M – ${high:,.0f}M)"
        )


class CompsAnalysis:
    """
    Comparable company analysis engine.

    Parameters
    ----------
    market_data_client : MarketDataClient | None
        Client for fetching peer profiles. With none, every symbol is left out
        and named in ``CompsResult.unavailable``: no multiple is ever invented.

    Examples
    --------
    >>> from finsight.ingestion.market_data import MarketDataClient
    >>> comps = CompsAnalysis(MarketDataClient())
    >>> result = comps.analyze("AAPL", peers=["MSFT", "GOOGL", "META"])
    >>> print(result.implied_ev_m)   # {'low': ..., 'mid': ..., 'high': ...} in $M
    """

    # Multiples computed for every company in the peer set. The subset that can
    # actually drive an implied valuation is `_MULTIPLE_BASIS` below — `pb` and
    # `p_fcf` are computed for comparison only and are not reported as
    # valuation drivers.
    MULTIPLES = ["ev_revenue", "ev_ebitda", "pe", "ps", "pb", "p_fcf"]

    def __init__(self, market_data_client=None) -> None:
        self.mdc = market_data_client

    def analyze(
        self,
        ticker: str,
        peers: list[str] | None = None,
        kpis=None,
    ) -> CompsResult:
        """
        Run comparable company analysis.

        Parameters
        ----------
        ticker : str
            Target company ticker.
        peers : list[str] | None
            Peer tickers. If None, auto-detects from market_data_client.
        kpis : ExtractedKPIs | None
            Pre-extracted KPIs for the target. If None, fetches from market data.

        Returns
        -------
        CompsResult
        """
        if peers is None and self.mdc is not None:
            peers = self.mdc.get_peers(ticker)
        if not peers:
            logger.warning(f"No peers found for {ticker}.")
            peers = []

        all_tickers = [ticker] + peers
        profiles: dict[str, dict] = {}
        unavailable: dict[str, str] = {}
        sectors: dict[str, str] = {}
        for sym in all_tickers:
            try:
                if self.mdc is None:
                    raise LookupError("no market-data client configured")
                profile = self.mdc.get_company_profile(sym)
                sectors[sym] = str(getattr(profile, "sector", "") or "").strip().lower()
                mismatch = self._currency_mismatch(profile)
                if mismatch:
                    logger.warning(f"{sym}: {mismatch}. Left out.")
                    unavailable[sym] = mismatch
                    continue
                multiples = self._profile_to_multiples(profile)
                # Yahoo can answer with an empty profile rather than an error.
                # That is no data too: it must be named, not counted as a peer.
                if not any(m in multiples for m in self.MULTIPLES):
                    logger.warning(f"{sym}: the market-data profile holds no multiple. Left out.")
                    unavailable[sym] = "market data retrieved, but its profile holds no multiple"
                    continue
                profiles[sym] = multiples
            except Exception as e:
                logger.warning(f"Could not fetch profile for {sym}: {e}. Left out.")
                unavailable[sym] = f"market data not retrieved ({type(e).__name__})"

        target_multiples = profiles.get(ticker, {})
        peer_data = {sym: v for sym, v in profiles.items() if sym != ticker}

        peer_df = pd.DataFrame(peer_data).T if peer_data else pd.DataFrame()

        # Compute peer statistics over the multiples that mean something: a
        # non-positive or non-finite multiple is excluded per metric (and
        # reported), never averaged in.
        peer_stats = pd.DataFrame()
        excluded: list[dict[str, Any]] = []
        if not peer_df.empty:
            usable, excluded = self._usable_multiples(peer_df)
            peer_stats = pd.DataFrame({
                "mean": usable.mean(),
                "median": usable.median(),
                "25th": usable.quantile(0.25),
                "75th": usable.quantile(0.75),
                "min": usable.min(),
                "max": usable.max(),
            })

        # Implied valuation. `kpis`, when supplied, is the better source for the
        # target's EBITDA and net income than a market-data profile — and it is
        # what lets the EV/EBITDA and P/E multiples contribute at all.
        anchors = self._target_anchors(target_multiples, kpis)
        skipped: dict[str, str] = {}
        limitations = (getattr(kpis, "provenance", None) or {}).get("limitations", {}) if kpis else {}
        if "depreciation_amortization" in limitations and anchors.get("ebitda_m"):
            # Peers' Yahoo EV/EBITDA is on full D&A; this target's 10-K EBITDA
            # is operating income + PP&E depreciation only, so applying one to
            # the other would understate the implied EV.
            anchors["ebitda_m"] = 0.0
            skipped["ev_ebitda"] = (
                "not applied: the target's EBITDA is operating income + PP&E "
                "depreciation only (no total D&A reported), while the peers' "
                "EV/EBITDA uses full D&A, so the two are on different bases"
            )
        ev_refused = self._ev_refusal(ticker, sectors)
        if ev_refused:
            skipped.update(dict.fromkeys(("ev_revenue", "ev_ebitda"), f"not applied: {ev_refused}"))
        implied = self._compute_implied_valuations(
            ticker, target_multiples, peer_stats, anchors, skip=skipped
        )

        return CompsResult(
            target_ticker=ticker,
            target_multiples=target_multiples,
            peer_multiples=peer_df,
            peer_stats=peer_stats,
            implied_valuations=implied,
            implied_ev_m=self._compute_range(implied, basis="ev"),
            implied_equity_m=self._compute_range(implied, basis="equity"),
            excluded_multiples=excluded,
            skipped_multiples=skipped,
            unavailable=unavailable,
            ev_refused=ev_refused,
        )

    @staticmethod
    def _ev_refusal(ticker: str, sectors: dict[str, str]) -> str | None:
        """Why a financial company gets no enterprise value, or None for any other.

        A bank's deposits and borrowings are what it works with, not financing to net off,
        so enterprise value does not describe it and an EV multiple implies nothing. The
        target's own sector decides; without one (its profile failed or left it blank),
        most of its peers do, so a Yahoo gap on the target cannot let the figure through.
        """
        known = sectors.get(ticker) or ""
        if not known:
            peers = [s for sym, s in sectors.items() if sym != ticker and s]
            financial = sum(s in _FINANCIAL_SECTORS for s in peers)
            if not financial or financial * 2 <= len(peers):
                return None
        elif known not in _FINANCIAL_SECTORS:
            return None
        return (f"{ticker} is a financial company, and for one enterprise value does not mean "
                "what it means elsewhere: deposits and borrowings are what it works with, not "
                "financing to net off. Price multiples still apply")

    @classmethod
    def _usable_multiples(
        cls, peer_df: pd.DataFrame
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Peer data with every non-positive / non-finite MULTIPLE blanked out.

        Only the valuation multiples are screened: a negative operating margin
        is a real figure, but a negative EV/EBITDA or P/E is a loss, not a
        price for a dollar of earnings, and a multiple of zero or infinity is
        not a price either. Returns the screened frame and one record per
        exclusion, so the caller can say which peers were left out and why.
        """
        usable = peer_df.apply(pd.to_numeric, errors="coerce").astype(float)
        excluded: list[dict[str, Any]] = []
        for multiple in cls.MULTIPLES:
            if multiple not in usable.columns:
                continue
            for ticker, value in usable[multiple].items():
                if pd.isna(value):
                    continue  # not reported: nothing to exclude
                if not np.isfinite(value):
                    reason = "non-finite"
                elif value < 0:
                    reason = "negative (the peer lost money on this line)"
                elif value == 0:
                    reason = "zero"
                else:
                    continue
                excluded.append({
                    "ticker": str(ticker),
                    "multiple": multiple,
                    "value": float(value),
                    "reason": reason,
                })
                usable.loc[ticker, multiple] = np.nan
        # Non-finite values in any other column are not data either.
        usable = usable.replace([np.inf, -np.inf], np.nan)
        return usable, excluded

    @staticmethod
    def _target_anchors(target: dict, kpis=None) -> dict[str, float]:
        """The target's own $M financials that peer multiples get applied to.

        Prefers extracted KPIs (filing-derived) over the market-data profile.
        """
        def positive(value) -> float:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return 0.0
            return v if v > 0 else 0.0

        anchors = {
            "revenue_m": positive(target.get("revenue_ttm_m")),
            "ebitda_m": positive(target.get("ebitda_ttm_m")),
            "net_income_m": positive(target.get("net_income_ttm_m")),
        }
        if kpis is not None:
            for key, attr in (("revenue_m", "revenue"), ("ebitda_m", "ebitda"),
                              ("net_income_m", "net_income")):
                from_kpis = positive(getattr(kpis, attr, None))
                if from_kpis:
                    anchors[key] = from_kpis
        return anchors

    @staticmethod
    def _currency_mismatch(profile) -> str | None:
        """Why a company's multiples are not comparable, if it is priced and
        reports in different currencies.

        EV and market cap are in the price currency; revenue, EBITDA and free
        cash flow in the reporting currency. For an ADR like TSM (USD price, TWD
        statements) EV/revenue would divide dollars by New Taiwan dollars, and
        nothing here applies an exchange rate, so the whole company is left out
        and named (Yahoo's own P/S and EV/EBITDA cannot be checked either). A
        profile that does not state both currencies cannot be tested and is used.
        """
        price, reported = getattr(profile, "currency", None), getattr(profile, "financial_currency", None)
        if not price or not reported or str(price).upper() == str(reported).upper():
            return None
        return (f"priced in {str(price).upper()} but reports in {str(reported).upper()}: its "
                "multiples would mix currencies, and no exchange rate is applied")

    def _profile_to_multiples(self, profile) -> dict[str, float]:
        """Extract multiples from a CompanyProfile."""
        m: dict[str, float] = {}

        def safe(val) -> float | None:
            return float(val) if val is not None and not np.isnan(float(val)) else None

        # EV/Revenue only from an EV and a revenue: P/S is a price multiple, and is no
        # substitute under this name.
        m["ev_revenue"] = safe(
            profile.enterprise_value / profile.revenue_ttm
            if profile.revenue_ttm and profile.revenue_ttm > 0 and profile.enterprise_value
            else None
        )
        m["ev_ebitda"] = safe(profile.ev_ebitda)
        m["pe"] = safe(profile.pe_ratio)
        m["ps"] = safe(profile.ps_ratio)
        m["pb"] = safe(profile.pb_ratio)
        m["p_fcf"] = safe(
            profile.market_cap / profile.free_cash_flow
            if profile.free_cash_flow and profile.free_cash_flow > 0
            else None
        )
        m["gross_margin"] = safe(profile.gross_margin)
        m["operating_margin"] = safe(profile.operating_margin)
        m["revenue_ttm_m"] = safe(profile.revenue_ttm / 1e6 if profile.revenue_ttm else None)
        m["market_cap_b"] = safe(profile.market_cap / 1e9 if profile.market_cap else None)
        # Anchors the EV/EBITDA and P/E multiples are applied to. Without these
        # two, those multiples can be displayed but never valued off.
        m["ebitda_ttm_m"] = safe(profile.ebitda_ttm / 1e6 if profile.ebitda_ttm else None)
        m["net_income_ttm_m"] = safe(
            profile.net_margin * profile.revenue_ttm / 1e6
            if profile.net_margin and profile.revenue_ttm
            else None
        )

        return {k: v for k, v in m.items() if v is not None}

    # Which target financial each multiple is applied to, and what the product
    # is dimensionally. EV multiples imply an ENTERPRISE value; price multiples
    # imply an EQUITY value. Every multiple the report displays appears here —
    # if you add a row to the report table, add it here too, or the report will
    # advertise a multiple that can never contribute to a valuation.
    _MULTIPLE_BASIS: dict[str, tuple[str, str]] = {
        "ev_revenue": ("revenue_m", "ev"),
        "ev_ebitda": ("ebitda_m", "ev"),
        "pe": ("net_income_m", "equity"),
        "ps": ("revenue_m", "equity"),
    }

    def _compute_implied_valuations(
        self,
        ticker: str,
        target: dict,
        peer_stats: pd.DataFrame,
        anchors: dict[str, float],
        skip: Any = (),
    ) -> dict[str, dict[str, Any]]:
        """
        For each multiple, compute the implied target value if the target traded
        at the peer median/mean multiple.

        Each entry records its ``basis``: ``"ev"`` for enterprise-value
        multiples, ``"equity"`` for price multiples. Previously EV/EBITDA and
        P/E were computed, displayed, and then structurally excluded here — the
        loop walked all four multiples but the body only handled the two applied
        to revenue — so two of the four multiples the report advertises could
        never move the valuation. P/S was also folded into the "implied
        enterprise value", though a price multiple implies equity, not EV.
        """
        implied: dict[str, dict[str, Any]] = {}

        if peer_stats.empty:
            return implied

        for multiple, (anchor_key, basis) in self._MULTIPLE_BASIS.items():
            if multiple not in peer_stats.index or multiple in skip:
                continue
            median_mult = peer_stats.loc[multiple, "median"]
            mean_mult = peer_stats.loc[multiple, "mean"]
            # A multiple must be a positive, finite price for a dollar of the
            # anchor; anything else would imply a negative (or no) value.
            if pd.isna(median_mult) or not np.isfinite(median_mult) or median_mult <= 0:
                continue
            if pd.isna(mean_mult) or not np.isfinite(mean_mult) or mean_mult <= 0:
                mean_mult = median_mult

            anchor = anchors.get(anchor_key, 0.0)
            if anchor <= 0:
                # No target financial to apply this multiple to. Skipped for a
                # data reason, not a structural one.
                logger.debug(
                    f"{ticker}: skipping {multiple} — no positive {anchor_key}"
                )
                continue

            implied[multiple] = {
                "median": float(median_mult) * anchor,  # $M
                "mean": float(mean_mult) * anchor,
                "basis": basis,
            }

        return implied

    def _compute_range(
        self,
        implied: dict,
        basis: str = "ev",
    ) -> dict[str, float]:
        """Compute low/mid/high implied value (in $M) for one basis.

        ``basis="ev"`` aggregates only the enterprise-value multiples and
        ``basis="equity"`` only the price multiples. They are never mixed: the
        result is a total company value in $M, NOT a per-share figure — callers
        must convert (EV: subtract net debt then divide by shares; equity:
        divide by shares) before comparing against a stock price.
        """
        values = [
            v
            for result in implied.values()
            if result.get("basis") == basis
            for k, v in result.items()
            if k in ("median", "mean") and v and v > 0
        ]
        if not values:
            return {"low": 0.0, "mid": 0.0, "high": 0.0}

        arr = np.array(values)
        return {
            "low": float(np.percentile(arr, 25)),
            "mid": float(np.median(arr)),
            "high": float(np.percentile(arr, 75)),
        }

"""
Response builders shared by the live API path (``app.py``) and demo mode
(``demo_data.py``).

Both paths must emit the same shape with the same honesty fields — ``mode``,
provenance, and the synthetic-source flags — so the shape lives here once
instead of being maintained twice and drifting.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from finsight.ingestion.transcripts import TRANSCRIPTS_OFF_DETAIL
from finsight.report.generator import stale_valuation_note, valuation_date

SYNTHETIC_TRANSCRIPT_SOURCE = (
    "synthetic placeholder transcript — boilerplate text identical for every "
    "company, not an earnings call"
)
SYNTHETIC_SENTIMENT_DETAIL = (
    "No real earnings-call transcript was analysed: the sentiment below is "
    "computed from a synthetic placeholder transcript and is illustrative only."
)


def sentiment_row(transcript: Any, sentiment: Any) -> dict[str, Any]:
    """One /sentiment result, tagged with whether its transcript is real."""
    synthetic = bool(getattr(transcript, "synthetic", False))
    return {
        "quarter": transcript.quarter,
        "overall_tone": sentiment.overall.sentiment_label,
        "net_sentiment": round(sentiment.overall.net_sentiment, 4),
        "guidance_tone": sentiment.guidance_tone,
        "risk_mentions": sentiment.risk_mentions,
        "sentiment_drift": (None if sentiment.sentiment_drift is None
                            else round(sentiment.sentiment_drift, 4)),
        "synthetic": synthetic,
        "source": SYNTHETIC_TRANSCRIPT_SOURCE if synthetic else transcript.url,
    }


def sentiment_payload(ticker: str, rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """The /sentiment response. ``synthetic`` is true if ANY result is."""
    synthetic = any(r["synthetic"] for r in rows)
    payload: dict[str, Any] = {
        "ticker": ticker,
        "mode": mode,
        "available": True,
        "synthetic": synthetic,
        "results": rows,
    }
    if synthetic:
        payload["detail"] = SYNTHETIC_SENTIMENT_DETAIL
    return payload


def sentiment_unavailable_payload(ticker: str) -> dict[str, Any]:
    """The /sentiment response with live transcripts off: no result, and why. Not the
    placeholder: nothing was tried, so there is nothing for it to stand in for."""
    return {
        "ticker": ticker,
        "mode": "live",
        "available": False,
        "synthetic": False,
        "results": [],
        "detail": TRANSCRIPTS_OFF_DETAIL,
    }


VALUATION_DATE_BASIS = (
    "fiscal year-end of the base 10-K: projected cash flows are discounted to "
    "this date and net debt is as of it. The value is not rolled forward, so a "
    "price quoted on a later date is compared across two dates."
)



def kpis_payload(kpis: Any, mode: str) -> dict[str, Any]:
    """The /kpis response: every extracted field, its provenance, and the mode."""
    return {**kpis.to_dict(), "mode": mode}


def total_debt_concepts(kpis: Any) -> list[str]:
    """The us-gaap concepts total debt was made of; empty when no debt figure was found."""
    concepts = (getattr(kpis, "provenance", None) or {}).get("concepts") or {}
    return re.findall(r"[A-Z][A-Za-z]+", concepts.get("total_debt") or "")


def dcf_payload(
    ticker: str, kpis: Any, result: Any, n_years: int, mode: str, price_date: str | None = None,
) -> dict[str, Any]:
    """The /dcf response.

    Beyond the valuation, it says what the valuation stands on: the fiscal
    year and filing (``provenance``), every assumption actually used, where
    each came from (``assumption_sources``), which were model defaults rather
    than reported figures (``defaulted_inputs``), and every input the
    projection moves away from its reported starting point (``adjustments``:
    a clamp, the CapEx and growth fades, an unreported side counted as 0).

    ``price_date`` is the demo's bundled close. The live route fetches no price, so its
    staleness check runs against today, the day any live price would be quoted.
    """
    a = result.assumptions
    limitations = dict(getattr(a, "limitations", {}) or {})
    stale = stale_valuation_note(
        valuation_date(kpis), price_date or date.today().isoformat(),
        "price date" if price_date else "date of this request")
    if stale:
        limitations["stale_valuation"] = stale
    return {
        "ticker": ticker,
        "mode": mode,
        "fiscal_year": kpis.period,
        # The date the valuation is struck at — NOT today and NOT the price
        # date: compare the per-share value with a later price knowing that.
        "valuation_date": valuation_date(kpis),
        "valuation_date_basis": VALUATION_DATE_BASIS,
        "intrinsic_value_per_share": result.intrinsic_value_per_share,
        "enterprise_value_m": result.enterprise_value,
        "equity_value_m": result.equity_value,
        "terminal_value_pct": result.terminal_value_pct,
        "pv_fcfs_m": result.pv_fcfs,
        "pv_terminal_value_m": result.pv_terminal_value,
        "projected_revenues_m": result.projected_revenues,
        "projected_fcfs_m": result.projected_fcfs,
        "assumptions": {
            "wacc": a.wacc,
            "terminal_growth_rate": a.terminal_growth_rate,
            "n_years": n_years,
            "revenue_growth_rates": list(a.revenue_growth_rates),
            "ebitda_margin": a.ebitda_margin,
            "da_as_pct_revenue": a.da_as_pct_revenue,
            "capex_as_pct_revenue": a.capex_as_pct_revenue,
            "terminal_capex_as_pct_revenue": a.terminal_capex_as_pct_revenue,
            "capex_pct_schedule": list(result.projected_capex_pct),
            "nwc_pct_of_revenue_change": a.nwc_pct_of_revenue_change,
            "tax_rate": a.tax_rate,
            "base_revenue_m": a.base_revenue,
            "net_debt_m": a.net_debt,
            "shares_outstanding_m": a.shares_outstanding,
        },
        # The tags total debt was read from. A filer using others reads low: see limitations.
        "total_debt_concepts": total_debt_concepts(kpis),
        "assumption_sources": dict(a.sources),
        "defaulted_inputs": defaulted_inputs(a),
        # The subset of defaulted_inputs that are COMPANY figures the filing
        # did not report (not the tunable WACC / growth / NWC / tax).
        "assumed_fundamentals": list(getattr(a, "assumed_fundamentals", [])),
        "adjustments": dict(a.adjustments),
        # What the inputs cannot show (e.g. depreciation-only D&A, finance
        # leases outside CapEx and debt) — disclosed, not corrected for.
        "limitations": limitations,
        "provenance": kpis.provenance,
    }


def dcf_source_lines(a: Any) -> list[str]:
    """The report's Data Sources lines for a DCF: its defaults and adjustments."""
    lines = []
    defaults = defaulted_inputs(a)
    if defaults:
        lines.append("DCF model defaults (not reported figures): " + ", ".join(defaults))
    for name, what in sorted(a.adjustments.items()):
        lines.append(f"DCF adjustment to a reported figure — {name}: {what}")
    for name, what in sorted((getattr(a, "limitations", {}) or {}).items()):
        lines.append(f"DCF input limitation — {name}: {what}")
    return lines


def defaulted_inputs(a: Any) -> list[str]:
    """Inputs that are model defaults rather than reported figures."""
    return sorted(k for k, v in a.sources.items() if v.startswith("default"))


def with_fallback_reason(
    payload: dict[str, Any], exc: BaseException, what: str = "The live SEC EDGAR path",
) -> dict[str, Any]:
    """Mark a demo payload served because the live path failed, and say why."""
    return {
        **payload,
        "fallback_reason": (
            f"{what} failed ({type(exc).__name__}); this is the bundled demo "
            "data instead."
        ),
    }

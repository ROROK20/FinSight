"""
Research report generator.

Assembles KPIs, DCF results, comps, and sentiment into a structured
equity research report in Markdown and JSON formats.

The report states the DCF **model value** beside the price and the gap between
them. It does not rate the stock or set a price target: the model value is a
mechanical output of one fiscal year's reported figures and a handful of
assumptions (by default a generic 10% WACC and 3% terminal growth), and an
automated "Buy"/"Sell" on a real company would present that arithmetic as a
recommendation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class EquityReport:
    """Full equity research report for a company."""

    ticker: str
    company_name: str
    report_date: str
    analyst: str = "FinSight"
    model_value_per_share: float | None = None
    """The DCF model's equity value per share — not a price target."""
    current_price: float | None = None
    valuation_date: str | None = None
    """The date the DCF is struck at: the base 10-K's fiscal year-end. Cash
    flows are discounted to it and net debt is taken as of it."""
    price_date: str | None = None
    """The date of ``current_price``. Usually later than ``valuation_date``;
    the model value is not rolled forward to it."""

    # Content sections
    investment_thesis: str = ""
    key_highlights: list[str] = field(default_factory=list)
    financial_summary: dict[str, Any] = field(default_factory=dict)
    dcf_summary: dict[str, Any] = field(default_factory=dict)
    comps_summary: dict[str, Any] = field(default_factory=dict)
    sentiment_summary: dict[str, Any] = field(default_factory=dict)
    key_risks: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)

    @property
    def date_gap_days(self) -> int | None:
        """Days from the valuation date to the price date (None if unknown)."""
        try:
            return (date.fromisoformat(self.price_date)
                    - date.fromisoformat(self.valuation_date)).days
        except (TypeError, ValueError):
            return None

    comps_unavailable: str | None = None
    """Why there is no comparable-company analysis, when the report was asked for
    one and has none. It reads "Unavailable", never a silently absent section."""

    sentiment_unavailable: str | None = None
    """Why there is no earnings-sentiment analysis, when it was not attempted."""

    comparison_withheld: str | None = None
    """Why the model-vs-price gap is not shown (e.g. a stock split between the
    share count's filing and the price), when it is not."""

    @property
    def model_vs_price(self) -> float | None:
        """(model value − price) / price: how far the model's arithmetic sits
        from the market price. Not an expected return."""
        if self.comparison_withheld:
            return None
        if self.model_value_per_share and self.current_price and self.current_price > 0:
            return (self.model_value_per_share - self.current_price) / self.current_price
        return None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    def __repr__(self) -> str:
        return (
            f"EquityReport({self.ticker} | model value=${self.model_value_per_share} | "
            f"price=${self.current_price} | {self.report_date})"
        )


def _finite(value: Any) -> Any:
    """``value`` with every non-finite float as None. JSON has no Infinity or NaN, and
    strict parsers refuse a literal one."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def split_between(shares_filed: str | None, price_date: str | None,
                  splits: list[dict] | None) -> str | None:
    """A reason to withhold model-vs-price when a stock split falls after the
    share count's 10-K was filed (and on or before the price date): the
    per-share value is on the pre-split count, the price split-adjusted. A
    10:1 split would otherwise read as a ~−90% gap.

    ``splits`` of None means the history is unknown, not that there are none: that
    withholds too, as the dashboard does. Pass ``[]`` when there are none.
    """
    if splits is None:
        return ("the split history could not be retrieved, so a stock split since the "
                "share count's 10-K cannot be ruled out and the two are not compared")
    if not shares_filed or not splits:
        return None
    hits = [s for s in splits
            if s.get("date") and s["date"] > shares_filed
            and (not price_date or s["date"] <= price_date) and s.get("ratio", 1) != 1]
    if not hits:
        return None
    what = ", ".join(f"{s['ratio']:g}:1 on {s['date']}" for s in hits)
    return (f"stock split ({what}) after the share count's 10-K (filed {shares_filed}): "
            "the per-share value is on the pre-split count and the price is "
            "split-adjusted, so the two are not compared")


def valuation_date(kpis: Any) -> str | None:
    """The DCF's valuation date: the base 10-K's fiscal year-end, if known."""
    provenance = getattr(kpis, "provenance", None) or {}
    return provenance.get("fiscal_year_end") or None


STALE_VALUATION_DAYS = 548
"""About 18 months. A filer's latest 10-K normally covers a year that ended at most about
15 months ago: twelve to the next year-end and up to 90 days to file."""


def stale_valuation_note(
    valuation: str | None, as_of: str | None, what: str = "price date",
) -> str | None:
    """A limitation when the valuation date is over STALE_VALUATION_DAYS before ``as_of``.

    "Latest" means the latest year with annual revenue in the filer's 10-K facts, so a
    filer whose revenue tags stopped in 2014 is valued as of 2014. It is said, not
    corrected: the value does not change.
    """
    try:
        days = (date.fromisoformat(as_of) - date.fromisoformat(valuation)).days
    except (TypeError, ValueError):
        return None
    if days <= STALE_VALUATION_DAYS:
        return None
    return (
        f"The valuation date {valuation} is {days:,} days before the {what} {as_of}, past the "
        f"{STALE_VALUATION_DAYS} days (about 18 months) a latest 10-K normally lags. The value "
        "describes that fiscal year, not the company now. Unless you asked for this year, the "
        "filer's annual revenue facts stop here: it may have moved to a concept FinSight does "
        "not read, or stopped filing 10-Ks."
    )


class ReportGenerator:
    """
    Generates equity research reports from analysis components.

    Examples
    --------
    >>> generator = ReportGenerator()
    >>> report = generator.generate(
    ...     ticker="AAPL",
    ...     kpis=kpis,
    ...     dcf_result=dcf_result,
    ...     comps_result=comps_result,
    ...     sentiment=transcript_sentiment,
    ... )
    >>> markdown = generator.to_markdown(report)
    >>> generator.save_json(report, "reports/AAPL_research.json")
    """

    def generate(
        self,
        ticker: str,
        kpis: Any = None,
        dcf_result: Any = None,
        comps_result: Any = None,
        sentiment: Any = None,
        company_name: str = "",
        comps_unavailable: str | None = None,
        current_price: float | None = None,
        sentiment_synthetic: bool = False,
        data_sources: list[str] | None = None,
        price_date: str | None = None,
        splits: list[dict] | None = None,
        sentiment_unavailable: str | None = None,
    ) -> EquityReport:
        """
        Assemble all analysis components into an EquityReport.

        Parameters
        ----------
        ticker : str
            Company ticker.
        kpis : ExtractedKPIs | None
            Extracted financial metrics.
        dcf_result : DCFResult | None
            DCF valuation output.
        comps_result : CompsResult | None
            Comps analysis output.
        comps_unavailable : str | None
            Why there is no comps output, printed in the comps section. Leave it
            None for a report that omits the section and says so elsewhere (demo).
        sentiment : TranscriptSentiment | None
            Sentiment analysis output.
        company_name : str
            Company name.
        current_price : float | None
            Current stock price, set beside the DCF model value.
        sentiment_synthetic : bool
            True when ``sentiment`` was computed from a synthetic placeholder
            transcript; the report then says so in the sentiment section.
        data_sources : list[str] | None
            One line per input source (filing, price, transcript), rendered
            as a "Data Sources" section.
        price_date : str | None
            ISO date of ``current_price``. Printed beside the valuation date
            (the base fiscal year-end) so the model-vs-price gap is read as a
            comparison across two dates, which it is.
        splits : list[dict] | None
            Stock splits (``[{"date", "ratio"}]``, as
            ``MarketDataClient.get_splits`` returns them); ``[]`` when there are
            none. None means the history is unknown, and withholds model vs price.
        sentiment_unavailable : str | None
            Why there is no sentiment analysis, printed in its section (live
            transcripts are off).

        Returns
        -------
        EquityReport
        """
        report = EquityReport(
            ticker=ticker.upper(),
            # No invented legal name: without one the ticker stands alone.
            company_name=company_name or ticker.upper(),
            report_date=str(date.today()),
            current_price=current_price,
            data_sources=list(data_sources or []),
            price_date=price_date,
            comps_unavailable=comps_unavailable,
            sentiment_unavailable=sentiment_unavailable,
        )

        # Financial summary
        if kpis is not None:
            report.financial_summary = {
                "period": getattr(kpis, "period", None),
                "revenue_m": kpis.revenue,
                "revenue_growth_yoy": kpis.revenue_growth_yoy,
                "gross_margin": kpis.gross_margin,
                "operating_margin": kpis.operating_margin,
                "net_margin": kpis.net_margin,
                "ebitda_m": kpis.ebitda,
                "eps_diluted": kpis.eps_diluted,
                "free_cash_flow_m": kpis.free_cash_flow,
                "net_debt_m": (kpis.total_debt or 0) - (kpis.cash_and_equivalents or 0),
            }
            report.key_risks = kpis.key_risks
            report.catalysts = kpis.growth_drivers

        # DCF summary
        if dcf_result is not None:
            a = dcf_result.assumptions
            report.dcf_summary = {
                "intrinsic_value_per_share": dcf_result.intrinsic_value_per_share,
                "enterprise_value_m": dcf_result.enterprise_value,
                "equity_value_m": getattr(dcf_result, "equity_value", None),
                "net_debt_m": getattr(a, "net_debt", None),
                "net_debt_assumed": a.sources.get("net_debt", "").startswith("default"),
                "shares_m": getattr(a, "shares_outstanding", None),
                "terminal_value_pct": dcf_result.terminal_value_pct,
                "wacc": a.wacc,
                "terminal_growth_rate": a.terminal_growth_rate,
                # Whether WACC / g were model defaults (an empty sources dict
                # means hand-built assumptions: the caller chose them).
                "wacc_default": a.sources.get("wacc", "").startswith("default"),
                "terminal_growth_default": a.sources.get(
                    "terminal_growth_rate", "").startswith("default"),
                "assumed_fundamentals": list(getattr(a, "assumed_fundamentals", [])),
                "limitations": dict(getattr(a, "limitations", {}) or {}),
                # Shares and EPS are as one filing reported them (not split-
                # adjusted): an older year can be on a pre-split basis.
                "per_share_basis": (getattr(kpis, "provenance", None) or {}).get("per_share_basis"),
                "projected_revenues": dcf_result.projected_revenues,
                "projected_fcfs": dcf_result.projected_fcfs,
            }
            if dcf_result.intrinsic_value_per_share > 0:
                report.model_value_per_share = round(dcf_result.intrinsic_value_per_share, 2)
            report.valuation_date = valuation_date(kpis)
            stale = stale_valuation_note(
                report.valuation_date, price_date or report.report_date,
                "price date" if price_date else "report date")
            if stale:
                report.dcf_summary["limitations"]["stale_valuation"] = stale
            if current_price is not None:  # with no price there is nothing to withhold
                report.comparison_withheld = split_between(
                    (getattr(kpis, "provenance", None) or {}).get("per_share_filed"),
                    price_date, splits)

        # Comps summary
        if comps_result is not None:
            report.comps_summary = {
                # Implied ENTERPRISE VALUE in $M (low/mid/high), NOT $/share.
                "implied_ev_m": comps_result.implied_ev_m,
                # Implied EQUITY value in $M from the price multiples (P/E, P/S),
                # kept separate — an equity value is not an enterprise value.
                "implied_equity_m": getattr(comps_result, "implied_equity_m", {}),
                "target_multiples": comps_result.target_multiples,
                "peer_median_multiples": comps_result.peer_stats["median"].to_dict()
                    if not comps_result.peer_stats.empty else {},
                # Peer multiples left out of the peer statistics (negative,
                # zero or non-finite), with the reason — printed below.
                "excluded_multiples": list(
                    getattr(comps_result, "excluded_multiples", [])
                ),
                "skipped_multiples": dict(getattr(comps_result, "skipped_multiples", {}) or {}),
                "ev_refused": getattr(comps_result, "ev_refused", None),
                "unavailable": dict(getattr(comps_result, "unavailable", {}) or {}),
                "peer_count": int(len(getattr(comps_result, "peer_multiples", []))),
            }
            # NOTE: comps here yields an implied ENTERPRISE VALUE (in $M), not a
            # per-share figure. Blending it into the $/share model value would
            # be dimensionally inconsistent. Converting EV → $/share needs net debt and a share
            # count, which the comps path does not carry, so the model value
            # stays DCF-per-share only. The implied EV is still surfaced in the
            # comps_summary above for context.

        # Sentiment summary
        if sentiment is not None:
            report.sentiment_summary = {
                "overall_tone": sentiment.overall.sentiment_label,
                "net_sentiment": round(sentiment.overall.net_sentiment, 4),
                "guidance_tone": getattr(sentiment, "guidance_tone", ""),
                "risk_mentions": getattr(sentiment, "risk_mentions", 0),
                "sentiment_drift": getattr(sentiment, "sentiment_drift", 0),
                "key_positives": sentiment.overall.key_positive_phrases[:3],
                "key_negatives": sentiment.overall.key_negative_phrases[:3],
                "synthetic": sentiment_synthetic,
            }

        # Generate the summary (no rating: see the module docstring)
        report.investment_thesis = self._generate_thesis(report)

        # Key highlights
        report.key_highlights = self._generate_highlights(report)

        return report

    def to_markdown(self, report: EquityReport) -> str:
        """
        Convert an EquityReport to formatted Markdown.

        Parameters
        ----------
        report : EquityReport

        Returns
        -------
        str
            Full Markdown-formatted research report.
        """
        def pct(v) -> str:
            return f"{v:.1%}" if v is not None else "N/A"

        def dollar(v, suffix="") -> str:
            if v is None:
                return "N/A"
            sign, v = ("-", -v) if v < 0 else ("", v)
            if v >= 1e9:
                return f"{sign}${v/1e9:.1f}B{suffix}"
            if v >= 1e6:
                return f"{sign}${v/1e6:.1f}M{suffix}"
            return f"{sign}${v:.2f}{suffix}"

        gap = report.model_vs_price
        gap_str = f"{gap:+.1%}" if gap is not None else "N/A"

        lines = [
            (f"# {report.company_name} ({report.ticker})"
             if report.company_name != report.ticker else f"# {report.ticker}"),
            f"**Automated Equity Research Report** | {report.report_date} | *{report.analyst}*",
            "",
            "| DCF model value | Price | Model value vs price |",
            "|-----------------|-------|----------------------|",
            f"| {self._model_value_str(report, dollar)} | {dollar(report.current_price)} | {gap_str} |",
            "",
            *self._dates_line(report),
            "> **A mechanical model output, not investment advice.** The model value "
            "is what FinSight's DCF computes from one fiscal year's reported figures "
            f"at the assumptions under *DCF Valuation*{self._basis_note(report)}. "
            "It is not a price target, a rating or a recommendation, and the gap to "
            "the price is not an expected return: change the assumptions and the "
            "value moves." + self._assumed_note(report),
            "",
            *self._current_debt_warning(report),
            "---",
            "",
            "## Summary",
            "",
            report.investment_thesis,
            "",
        ]

        if report.key_highlights:
            lines += ["## Key Highlights", ""]
            for h in report.key_highlights:
                lines.append(f"- {h}")
            lines.append("")

        if report.financial_summary:
            fs = report.financial_summary
            eps = fs.get("eps_diluted")
            eps_str = "N/A" if eps is None else f"${eps:.2f}"
            lines += [
                "## Financial Summary",
                "",
                "| Metric | Value |",
                "|--------|-------|",
                f"| Revenue ({fs.get('period') or 'FY'}) | {dollar(fs.get('revenue_m', 0) * 1e6 if fs.get('revenue_m') else None)} |",
                f"| Revenue Growth (YoY) | {pct(fs.get('revenue_growth_yoy'))} |",
                f"| Gross Margin | {pct(fs.get('gross_margin'))} |",
                f"| Operating Margin | {pct(fs.get('operating_margin'))} |",
                f"| Net Margin | {pct(fs.get('net_margin'))} |",
                f"| EBITDA | {dollar(fs.get('ebitda_m', 0) * 1e6 if fs.get('ebitda_m') else None)} |",
                f"| EPS (Diluted) | {eps_str} |",
                f"| Reported FCF (operating cash flow − CapEx) | {dollar(fs.get('free_cash_flow_m', 0) * 1e6 if fs.get('free_cash_flow_m') else None)} |",
                "",
            ]

        if report.dcf_summary:
            d = report.dcf_summary
            default = " (model default, not company-specific)"
            lines += [
                "## DCF Valuation",
                "",
                f"- **Intrinsic Value per Share (model):** {self._model_value_str(report, dollar)}",
                *([f"- **Per-share basis:** shares {d['per_share_basis']}"]
                  if d.get("per_share_basis") else []),
                f"- **Enterprise Value:** {dollar(d.get('enterprise_value_m', 0) * 1e6 if d.get('enterprise_value_m') else None)}",
                *self._to_per_share_lines(d, dollar),
                f"- **Terminal Value % of EV:** {pct(d.get('terminal_value_pct'))}",
                f"- **WACC:** {pct(d.get('wacc'))}{default if d.get('wacc_default') else ''}",
                f"- **Terminal Growth Rate:** {pct(d.get('terminal_growth_rate'))}"
                f"{default if d.get('terminal_growth_default') else ''}",
                "",
            ]

        if report.comps_summary:
            c = report.comps_summary
            ev = c.get("implied_ev_m", {})

            def ev_dollar(v_m) -> str:
                # Values are enterprise values already expressed in $M.
                return dollar(v_m * 1e6 if v_m else None)

            eq = c.get("implied_equity_m", {}) or {}

            lines += ["## Comparable Company Analysis", ""]
            unavailable = c.get("unavailable", {}) or {}
            if not c.get("peer_count"):
                # No peer data at all: say so and show nothing — no figure is
                # built from stand-in numbers.
                why = "; ".join(f"{t}: {r}" for t, r in sorted(unavailable.items())) \
                    or "no peer data"
                lines += [f"Unavailable: peer market data could not be retrieved ({why}). "
                          "No comparable-company figure is shown.", ""]
            else:
                # Peer multiples with nothing of the target's to apply them to.
                if c.get("ev_refused"):
                    lines.append("- **Implied Enterprise Value (EV multiples):** not computed: "
                                 f"{c['ev_refused']}.")
                elif any(ev.get(k) for k in ("low", "mid", "high")):
                    lines.append(
                        f"- **Implied Enterprise Value (EV multiples):** "
                        f"{ev_dollar(ev.get('low'))} — {ev_dollar(ev.get('mid'))} — {ev_dollar(ev.get('high'))}"
                    )
                else:
                    lines.append(
                        "- **Implied Enterprise Value (EV multiples):** not computed: no peer "
                        "multiple and positive target revenue or EBITDA to apply it to."
                    )
                if any(eq.get(k) for k in ("low", "mid", "high")):
                    lines.append(
                        f"- **Implied Equity Value (price multiples):** "
                        f"{ev_dollar(eq.get('low'))} — {ev_dollar(eq.get('mid'))} — {ev_dollar(eq.get('high'))}"
                    )
                target_period = (report.financial_summary or {}).get("period") or "fiscal-year"
                lines += [
                    "",
                    f"> **Periods differ.** Peer multiples are Yahoo Finance trailing-"
                    f"twelve-month (TTM) figures; they are applied to the target's "
                    f"{target_period} 10-K revenue, EBITDA and net income, which may "
                    "end months earlier.",
                ]
                lines.append("")

                target_m = c.get("target_multiples", {})
                peer_m = c.get("peer_median_multiples", {})
                if target_m or peer_m:
                    # "Drives valuation" makes the gate visible: a multiple shown
                    # here that cannot move the implied value says so, instead of
                    # looking like an input when it is only decoration.
                    lines += [
                        "| Multiple | Target | Peer Median | Drives valuation |",
                        "|----------|--------|-------------|------------------|",
                    ]
                    implied_basis = {
                        "ev_revenue": "Enterprise value",
                        "ev_ebitda": "Enterprise value",
                        "pe": "Equity value",
                        "ps": "Equity value",
                    }
                    def multiple_str(v) -> str:
                        # NaN (no usable peer) is truthy, so test finiteness too.
                        return f"{v:.1f}x" if v and math.isfinite(v) else "N/A"

                    skipped = c.get("skipped_multiples", {}) or {}
                    for mult in ["ev_revenue", "ev_ebitda", "pe", "ps"]:
                        t_str = multiple_str(target_m.get(mult))
                        p_str = multiple_str(peer_m.get(mult))
                        drives = "Not applied (see below)" if mult in skipped else implied_basis.get(mult, "—")
                        lines.append(
                            f"| {mult.replace('_', '/')} | {t_str} | {p_str} | {drives} |"
                        )
                    lines.append("")

                for mult, why in sorted((c.get("skipped_multiples", {}) or {}).items()):
                    lines += [f"{mult.replace('_', '/').upper()} {why}.", ""]

                excluded = c.get("excluded_multiples", []) or []
                if excluded:
                    # Say which peers did not count, so a median over fewer peers
                    # is never mistaken for one over the whole set.
                    lines.append(
                        "Excluded from the peer statistics (a multiple must be a "
                        "positive, finite number to say what a dollar of earnings "
                        "or sales is worth):"
                    )
                    for e in excluded:
                        value = e.get("value")
                        shown = f"{value:.1f}x" if value is not None and math.isfinite(value) else str(value)
                        lines.append(
                            f"- {e.get('ticker')} {str(e.get('multiple', '')).replace('_', '/')} "
                            f"{shown} — {e.get('reason')}"
                        )
                    lines.append("")
                if unavailable:
                    lines += ["Left out (no usable market data): " + "; ".join(
                        f"{t} — {r}" for t, r in sorted(unavailable.items())) + ".", ""]

        if report.comps_unavailable and not report.comps_summary:
            lines += ["## Comparable Company Analysis", "",
                      f"Unavailable: {report.comps_unavailable} "
                      "No comparable-company figure is shown.", ""]

        if report.sentiment_summary:
            s = report.sentiment_summary
            lines += ["## Earnings Sentiment Analysis", ""]
            if s.get("synthetic"):
                lines += [
                    "> ⚠️ **Synthetic transcript.** No real earnings-call "
                    "transcript was analysed: the figures below come from a "
                    "boilerplate placeholder that reads the same for every "
                    "company, and are illustrative only.",
                    "",
                ]
            lines += [
                f"- **Overall Tone:** {s.get('overall_tone', 'N/A')}",
                f"- **Net Sentiment Score:** {s.get('net_sentiment', 0):.3f}",
                f"- **Guidance Tone:** {s.get('guidance_tone', 'N/A').replace('_', ' ').title()}",
                f"- **Risk Mentions:** {s.get('risk_mentions', 0)}",
                "",
            ]
            # Only sentences that score net-positive / net-negative on the
            # lexicon are listed; an empty list is said out loud rather than
            # padded with whichever sentences scored least badly.
            for heading, key, none_found in (
                ("Key Positive Statements", "key_positives",
                 "none found — no sentence scores net-positive on the sentiment lexicon."),
                ("Key Concerns", "key_negatives",
                 "none found — no sentence scores net-negative on the sentiment lexicon."),
            ):
                phrases = s.get(key) or []
                if phrases:
                    lines.append(f"**{heading}:**")
                    lines += [f'> "{p}"' for p in phrases]
                else:
                    lines.append(f"**{heading}:** {none_found}")
                lines.append("")

        if report.sentiment_unavailable and not report.sentiment_summary:
            lines += ["## Earnings Sentiment Analysis", "", report.sentiment_unavailable, ""]

        if report.key_risks:
            lines += ["## Key Risks", ""]
            for risk in report.key_risks:
                lines.append(f"- {risk}")
            lines.append("")

        if report.catalysts:
            lines += ["## Growth Catalysts", ""]
            for cat in report.catalysts:
                lines.append(f"- {cat}")
            lines.append("")

        if report.data_sources:
            lines += ["## Data Sources", ""]
            lines += [f"- {src}" for src in report.data_sources]
            lines.append("")

        lines += [
            "---",
            "",
            "*This report was generated automatically by FinSight. Not investment advice.*",
            "*For informational purposes only. Verify all data independently.*",
        ]

        return "\n".join(lines)

    def save_markdown(self, report: EquityReport, path: str) -> None:
        """Save report as Markdown file."""
        md = self.to_markdown(report)
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)

    def save_json(self, report: EquityReport, path: str) -> None:
        """Save report as JSON file."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_finite(report.to_dict()), f, indent=2, default=str, allow_nan=False)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    @staticmethod
    def _model_value_str(report: EquityReport, dollar) -> str:
        """The per-share value, said the same way in the header and the DCF
        section."""
        iv = (report.dcf_summary or {}).get("intrinsic_value_per_share")
        if iv is None:
            return "N/A"
        if iv <= 0:
            return "Not meaningful (equity value ≤ 0)"
        return dollar(iv)

    @staticmethod
    def _to_per_share_lines(d: dict[str, Any], dollar) -> list[str]:
        """Enterprise value to per-share value, every step printed, so the two can be
        reconciled by hand: EV − net debt = equity value; equity value ÷ shares = value."""
        net_debt, equity, shares = d.get("net_debt_m"), d.get("equity_value_m"), d.get("shares_m")
        if net_debt is None or equity is None or not shares:
            return []
        how = ("a side not reported counted as 0: see the note above"
               if d.get("net_debt_assumed") else
               "total debt − cash and cash equivalents; marketable securities not netted")
        return [
            f"- **Net Debt:** {dollar(net_debt * 1e6)} ({how})",
            f"- **Equity Value:** {dollar(equity * 1e6)} (enterprise value − net debt)",
            f"- **Shares:** {shares:,.1f}M (equity value ÷ shares = the value per share)",
        ]

    @staticmethod
    def _assumed_note(report: EquityReport) -> str:
        """Name, beside the value, any company figure the model assumed."""
        assumed = (report.dcf_summary or {}).get("assumed_fundamentals") or []
        if not assumed:
            return ""
        return (" **It also rests on figures the filing did not report, which "
                f"the model assumed: {', '.join(assumed)}** (see Data Sources).")

    @staticmethod
    def _current_debt_warning(report: EquityReport) -> list[str]:
        """Beside the value, not only in Data Sources: it moves the per-share figure."""
        limitations = (report.dcf_summary or {}).get("limitations") or {}
        lines: list[str] = []
        for key, title in (("current_debt", "Current debt"),
                           ("long_term_debt_not_found", "Long-term debt"),
                           ("debt_above_total", "Total debt")):
            if limitations.get(key):
                lines += [f"> **{title}.** {limitations[key]}", ""]
        return lines

    @staticmethod
    def _dates_line(report: EquityReport) -> list[str]:
        """The two dates behind the comparison table, as report lines.

        The DCF is struck at the base fiscal year-end (cash flows discounted
        to it, net debt as of it) while the price is usually quoted months
        later, so the gap mixes two dates. Said beside the table, with the
        distance between them, instead of left for the reader to discover.
        """
        if not (report.valuation_date or report.price_date):
            return []
        valuation = (f"{report.valuation_date} (fiscal year-end of the base 10-K: "
                     "cash flows are discounted to it, net debt is as of it)"
                     if report.valuation_date else "unknown")
        price = report.price_date or "unknown"
        gap = report.date_gap_days
        apart = (f" — {gap} days apart; the model value is not rolled forward "
                 "to the price date" if gap is not None and gap > 0 else "")
        lines = [f"**Valuation date:** {valuation} · **Price date:** {price}{apart}.", ""]
        stale = ((report.dcf_summary or {}).get("limitations") or {}).get("stale_valuation")
        if stale:
            lines += [f"> **Stale valuation.** {stale}", ""]
        if report.comparison_withheld:
            lines += [f"**Model vs price withheld:** {report.comparison_withheld}.", ""]
        return lines

    @staticmethod
    def _basis_note(report: EquityReport) -> str:
        d = report.dcf_summary
        if not d:
            return ""
        defaults = d.get("wacc_default") or d.get("terminal_growth_default")
        return (
            f" — {d.get('wacc', 0):.1%} WACC and {d.get('terminal_growth_rate', 0):.1%} "
            "terminal growth" + (", generic model defaults" if defaults else "")
        )

    def _generate_thesis(self, report: EquityReport) -> str:
        """A factual summary of the figures; no view on the stock.

        Sentiment is quoted only when it came from a real transcript: tone
        computed from the synthetic placeholder says nothing about the
        company's management.
        """
        fs = report.financial_summary
        parts = []

        # Figures only, no adjectives: an adjective is a view, in a summary that claims none.
        if fs:
            rev_growth = fs.get("revenue_growth_yoy")
            gm = fs.get("gross_margin")
            if rev_growth is not None:
                parts.append(
                    f"revenue {'grew' if rev_growth >= 0 else 'fell'} "
                    f"{abs(rev_growth):.1%} year over year ({fs.get('period') or 'fiscal year'})"
                )
            if gm is not None:
                parts.append(f"gross margin was {gm:.1%}")

        s = report.sentiment_summary
        if s and not s.get("synthetic"):
            tone = s.get("overall_tone", "")
            gt = s.get("guidance_tone", "")
            if tone in ("Bullish", "Bearish"):
                parts.append(
                    f"the latest earnings call scores net {'positive' if tone == 'Bullish' else 'negative'} "
                    "on FinSight's sentiment word lists")
            if gt == "raised":
                parts.append("management raised guidance on that call")
            elif gt == "lowered":
                parts.append("management lowered guidance on that call")

        gap = report.model_vs_price
        if gap is not None:
            side = "above" if gap >= 0 else "below"
            parts.append(
                f"the DCF model value is {abs(gap):.0%} {side} the price at the "
                "assumptions stated below"
            )

        if not parts:
            return (
                f"{report.ticker}: not enough reported data for an automated "
                "summary."
            )

        sentences = [p[0].upper() + p[1:] for p in parts]
        return f"{report.ticker}: " + ". ".join(sentences) + "."

    def _generate_highlights(self, report: EquityReport) -> list[str]:
        highlights = []
        fs = report.financial_summary

        if fs:
            if fs.get("revenue_growth_yoy"):
                highlights.append(
                    f"Revenue growth of {fs['revenue_growth_yoy']:.1%} YoY"
                )
            if fs.get("gross_margin"):
                highlights.append(f"Gross margin of {fs['gross_margin']:.1%}")
            if fs.get("free_cash_flow_m"):
                fcf = fs["free_cash_flow_m"]
                highlights.append(
                    f"Reported free cash flow of {'-' if fcf < 0 else ''}${abs(fcf):,.0f}M"
                )

        iv = report.dcf_summary.get("intrinsic_value_per_share")
        if iv is not None and iv > 0:
            highlights.append(f"DCF model value: ${iv:.2f}/share")

        s = report.sentiment_summary
        if s.get("guidance_tone") == "raised" and not s.get("synthetic"):
            highlights.append("Management raised forward guidance")

        return highlights[:5]

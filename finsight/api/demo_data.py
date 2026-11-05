"""
Bundled demo / offline data for FinSight.

Demo mode runs the LIVE extraction and valuation code over recorded data, so it
makes ZERO network requests and needs no keys, while every figure stays
checkable against a public source:

* **Financials** — real SEC EDGAR companyfacts for each ticker, retrieved
  ``finsight.data.SNAPSHOT_RETRIEVED`` and bundled in
  ``finsight/data/companyfacts/``. ``KPIExtractor.extract_from_companyfacts``
  reads the latest fiscal year exactly as the live ``/kpis`` does, and every
  response cites the fiscal year, year-end and 10-K accession it used.
* **Prices** — the Yahoo Finance daily close on :data:`PRICE_DATE`, dated in
  every response that shows one. Not a live quote.
* **Sentiment** — demo mode bundles no transcripts, so sentiment is computed
  from the synthetic placeholder transcript and tagged ``synthetic: true``.

Activation:
  * FINSIGHT_DEMO=1 in the environment  → always serve the bundled data, and
    refuse any other ticker (no network).
  * Otherwise, the live EDGAR path runs first and this data is used only as a
    fallback when the network call fails (``fallback_reason`` says so).

Coverage: all six ticker endpoints (/meta, /filings, /kpis, /dcf, /sentiment,
/report) honour the flag, and demo mode is zero-network for EVERY ticker. Two
endpoints answer the bundled tickers by declining rather than by inventing
data: /filings serves an honest empty index (see demo_filings) and /report
omits its comparable-company section (peer multiples need live market data).
A ticker outside the bundled set is refused with a 404 whose detail is
:func:`not_bundled_detail` — it never falls through to a live SEC, Yahoo or
transcript request. Every JSON response is tagged ``"mode": "demo"``.

Supported tickers: AAPL, MSFT, NVDA, GOOGL, TSLA — chosen to match the
frontend's TICKERS list so the one-command demo always has data.
"""

from __future__ import annotations

import copy
import os
import re
from functools import lru_cache
from typing import Any

from finsight.api import payloads
from finsight.data import SNAPSHOT_RETRIEVED, companyfacts_snapshot, snapshot_meta
from finsight.extraction.kpi_extractor import ExtractedKPIs, KPIExtractor
from finsight.ingestion.transcripts import TranscriptFetcher
from finsight.models.dcf import DCFModel
from finsight.report.generator import ReportGenerator
from finsight.sentiment.analyzer import SentimentAnalyzer

PRICE_DATE = "2026-10-30"
PRICE_SOURCE = "Yahoo Finance daily close"

# Name and sector as Yahoo Finance reports them (what the live /meta returns),
# and the PRICE_DATE close. Everything else comes from the SEC snapshots.
_DEMO_META: dict[str, dict[str, Any]] = {
    "AAPL": {"company_name": "Apple Inc.", "sector": "Technology", "current_price": 344.63},
    "MSFT": {"company_name": "Microsoft Corporation", "sector": "Technology", "current_price": 521.91},
    "NVDA": {"company_name": "NVIDIA Corporation", "sector": "Technology", "current_price": 204.64},
    "GOOGL": {"company_name": "Alphabet Inc.", "sector": "Communication Services", "current_price": 279.84},
    "TSLA": {"company_name": "Tesla, Inc.", "sector": "Consumer Cyclical", "current_price": 399.81},
}

# What a filing shows and the extractor's evidence rule cannot. Tesla's LongTermDebt is the
# noncurrent part of its debt: at 2025-12-31 its balance sheet has $6,736M of noncurrent and
# $1,640M of current debt and finance leases, which less the FinanceLeaseLiability facts
# ($152M, $71M) are the snapshot's LongTermDebt ($6,584M) and DebtCurrent ($1,569M). Those
# two sheet lines are tsla: extension tags, which SEC's companyfacts leaves out of the
# us-gaap concepts the extractor reads, so its answer stays "unsettled" and nothing is added.
# The disclosure below rests on that answer, and stops if a filing ever settles it.
KNOWN_CURRENT_DEBT: dict[str, dict[str, Any]] = {
    "TSLA": {
        "company": "Tesla",
        "filing": "FY2025 10-K",
        "url": "https://www.sec.gov/Archives/edgar/data/1318605/000162828026003952/tsla-20251231.htm",
        "as_of": "2025-12-31",
        "current_m": 1_569.0,
        "long_term_m": 6_584.0,
    },
}

_DISCLOSURE_LEAD = "The demo's debt excludes current debt."

DEMO_TICKERS = list(_DEMO_META.keys())
DEMO_SOURCE = f"bundled SEC EDGAR companyfacts snapshot (retrieved {SNAPSHOT_RETRIEVED})"

_extractor = KPIExtractor()
_dcf_model = DCFModel()
_sentiment = SentimentAnalyzer()
_transcripts = TranscriptFetcher(cache=False)
_reporter = ReportGenerator()

_SYNTHETIC_QUARTERS = [f"Q{q} (synthetic)" for q in (4, 3, 2, 1)]


def demo_enabled() -> bool:
    """True when FINSIGHT_DEMO is set to a truthy value."""
    return os.getenv("FINSIGHT_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def has_demo(ticker: str) -> bool:
    return ticker.upper() in _DEMO_META


def not_bundled_detail(ticker: str) -> str:
    """Why demo mode refuses ``ticker`` and what to do instead."""
    return (
        f"{ticker.upper()} is not in the offline demo set "
        f"({', '.join(DEMO_TICKERS)}). Demo mode (FINSIGHT_DEMO=1) makes no "
        "network requests, so it cannot look up other companies. To analyse "
        "another company that files 10-Ks, run the API without FINSIGHT_DEMO and "
        "with SEC_EDGAR_USER_AGENT set to your own name and email. It reads 10-K XBRL "
        "facts only: a foreign filer on Form 20-F has none, and a company without the "
        "concepts it reads, such as a bank with no operating income, gets no DCF."
    )


@lru_cache(maxsize=None)
def _extract(ticker: str, period: str | None) -> ExtractedKPIs:
    return _extractor.extract_from_companyfacts(
        companyfacts_snapshot(ticker), ticker, period, source=DEMO_SOURCE
    )


def _trimmed_before(ticker: str) -> str:
    """The date the snapshot's facts start after (its ``contents`` states it)."""
    contents = snapshot_meta(companyfacts_snapshot(ticker))["contents"]
    return re.search(r"periods ending after (\d{4}-\d{2}-\d{2})", contents).group(1)


def _known_debt_gap(ticker: str, kpis: ExtractedKPIs) -> dict[str, Any] | None:
    """The measured gap, while the extractor says nothing settles whether LongTermDebt
    holds the current debt reported beside it."""
    note = ((kpis.provenance or {}).get("limitations") or {}).get("current_debt", "")
    unsettled = "but nothing here shows whether" in note or note.startswith(_DISCLOSURE_LEAD)
    return KNOWN_CURRENT_DEBT.get(ticker) if unsettled else None


def _debt_current_m(ticker: str, fy_end: str) -> float:
    """The snapshot's DebtCurrent at a fiscal year-end, $M."""
    rows = companyfacts_snapshot(ticker)["facts"]["us-gaap"]["DebtCurrent"]["units"]["USD"]
    return next(r["val"] for r in rows if r["end"] == fy_end) / 1e6


def _current_debt_disclosure(gap: dict[str, Any], kpis: ExtractedKPIs, result: Any = None) -> str:
    """The plain-words note: what the debt leaves out, the filing, and, given a DCF,
    what that does to its value."""
    lead = (
        f"{_DISCLOSURE_LEAD} {gap['company']}'s LongTermDebt tag, all that total debt counts, "
        f"is the noncurrent part only: its {gap['filing']} ({gap['url']}) shows "
        f"${gap['current_m']:,.0f}M of current debt beside ${gap['long_term_m']:,.0f}M long-term "
        f"at {gap['as_of']}."
    )
    value = None if result is None else result.intrinsic_value_per_share
    if value is None or value <= 0:
        return (f"{lead} Net debt is understated and any value built on it overstated "
                "(/dcf says by how much).")
    fy_end = kpis.provenance["fiscal_year_end"]
    current_m = _debt_current_m(kpis.ticker, fy_end)
    per_share = current_m / result.assumptions.shares_outstanding
    share = per_share / value
    effect = (
        f" Net debt is understated and the value overstated: at ${current_m:,.0f}M, by about "
        f"${per_share:,.2f} a share, about {share:.0%} of the ${value:,.2f} value."
        if share >= 0.01 else
        f" Net debt is understated and the value overstated: at ${current_m:,.0f}M, by about "
        f"${per_share:,.2f} a share, about {share:.1%} of the ${value:,.2f} value."
    )
    if fy_end != gap["as_of"]:
        effect += f" That ${current_m:,.0f}M is the DebtCurrent reported for {fy_end}."
    return lead + effect


def _disclose_debt_effect(ticker: str, kpis: ExtractedKPIs, result: Any) -> None:
    """Put the effect on this DCF's value beside the disclosure, wherever it is shown."""
    gap = _known_debt_gap(ticker, kpis)
    if gap:
        note = _current_debt_disclosure(gap, kpis, result)
        kpis.provenance["limitations"]["current_debt"] = note
        result.assumptions.limitations["current_debt"] = note


def _kpis_for(ticker: str, period: str | None = None) -> ExtractedKPIs:
    """KPIs from the bundled snapshot via the live extraction path.

    The snapshot's earliest year has no growth figure only because the file was
    trimmed before its prior year, and the response says that rather than imply SEC
    has none.

    Raises ``ValueError`` if ``period`` is not a fiscal year in the snapshot, and says
    it is the snapshot's limit, not the filer's.
    """
    t = ticker.upper()
    try:
        kpis = copy.deepcopy(_extract(t, period or None))
    except ValueError as e:
        if "No annual 10-K revenue reported" not in str(e):
            raise
        wanted = re.sub(r"^FY", "", period or "", flags=re.IGNORECASE)
        held = re.search(r"\(latest available: [^)]*\)", str(e))
        raise ValueError(
            f"No fiscal year {wanted} in {t}'s bundled demo snapshot, which was retrieved "
            f"{SNAPSHOT_RETRIEVED} and keeps only 10-K periods ending after "
            f"{_trimmed_before(t)}{' ' + held.group(0) if held else ''}. That says nothing "
            "about SEC EDGAR: unset FINSIGHT_DEMO to read the live data."
        ) from e
    if kpis.revenue_growth_yoy is None:
        kpis.provenance.setdefault("limitations", {})["revenue_growth_yoy"] = (
            f"Not available offline: the bundled snapshot keeps only periods ending after "
            f"{_trimmed_before(t)}, so {kpis.period}'s prior year was cut out of the file. "
            "The live path reads it from SEC EDGAR."
        )
    gap = _known_debt_gap(t, kpis)
    if gap:
        kpis.provenance["limitations"]["current_debt"] = _current_debt_disclosure(gap, kpis)
    return kpis


def demo_kpis(ticker: str, period: str | None = None) -> dict[str, Any]:
    """Demo /kpis response — identical shape to the live endpoint."""
    return payloads.kpis_payload(_kpis_for(ticker, period), mode="demo")


def demo_dcf(
    ticker: str,
    n_years: int = 5,
    wacc: float | None = None,
    terminal_growth: float | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Demo /dcf response — the REAL DCFModel over the snapshot's latest year
    (or ``period``'s, as ``/kpis`` selects it).

    ``wacc`` and ``terminal_growth`` mirror the ``/dcf`` query params: when
    provided they are threaded into the model so the returned assumptions
    reflect what was actually used (not the defaults).
    """
    t = ticker.upper()
    kpis = _kpis_for(t, period)
    result = DCFModel(n_years=n_years).value_from_kpis(
        kpis, wacc=wacc, terminal_growth=terminal_growth
    )
    _disclose_debt_effect(t, kpis, result)
    payload = payloads.dcf_payload(t, kpis, result, n_years, mode="demo", price_date=PRICE_DATE)
    if kpis.revenue_growth_yoy is None:
        sources = payload["assumption_sources"]
        sources["revenue_growth_rates"] = sources["revenue_growth_rates"].replace(
            "no prior-year revenue",
            f"the bundled snapshot keeps only periods ending after {_trimmed_before(t)}, so "
            f"{kpis.period}'s prior-year revenue was cut out of the file (the live path reads "
            "it from SEC EDGAR)",
        )
    return payload


def demo_sentiment(ticker: str, n_transcripts: int = 1) -> dict[str, Any]:
    """Demo /sentiment response — the REAL SentimentAnalyzer over the synthetic
    placeholder transcript.

    Demo mode bundles no transcripts, so every result is computed from the
    same boilerplate placeholder and is tagged ``synthetic: true``; the scores
    are therefore identical across tickers, which is the honest consequence of
    having no real call to analyse.
    """
    t = ticker.upper()
    rows = []
    for q in _SYNTHETIC_QUARTERS[:max(1, n_transcripts)]:
        transcript = _transcripts.create_mock_transcript(t, q)
        rows.append(payloads.sentiment_row(transcript, _sentiment.analyze_transcript(transcript)))
    return payloads.sentiment_payload(t, rows, mode="demo")


def demo_meta(ticker: str) -> dict[str, Any]:
    """Company metadata: name, sector, and the dated fixture price."""
    m = _DEMO_META[ticker.upper()]
    return {**m, "price_date": PRICE_DATE, "price_source": PRICE_SOURCE}


def demo_filings(ticker: str, form_type: str = "10-K", limit: int = 5) -> dict[str, Any]:
    """Demo /filings response.

    Filing *indexes* are deliberately NOT bundled. The demo fixtures carry the
    financial figures a filing reports, but an accession number, filing date and
    archive URL are verifiable public records — inventing plausible-looking ones
    would be fabricated data wearing the costume of a real SEC record, which is
    worse than serving nothing. So demo mode answers honestly and makes zero
    network calls: same response shape as the live endpoint, an empty filing
    list, and a ``detail`` that says why.

    Set ``FINSIGHT_DEMO=0`` (or unset it) to query real SEC EDGAR.
    """
    return {
        "ticker": ticker.upper(),
        "cik": None,
        "form_type": form_type,
        "count": 0,
        "filings": [],
        "mode": "demo",
        "detail": (
            "Filing indexes are not bundled with the offline demo — an accession "
            "number and archive URL are verifiable public records and this "
            "project does not ship invented ones. Unset FINSIGHT_DEMO to query "
            "real SEC EDGAR for filing metadata."
        ),
    }


def demo_report(ticker: str) -> str:
    """Demo /report response — the REAL ReportGenerator over the bundled data.

    Runs the same pipeline the live endpoint runs (KPIs → DCF → sentiment →
    ReportGenerator) with zero network access. The comparable-company section is
    omitted rather than faked: comps need live peer market data, and the only
    offline alternative would be synthetic peer multiples presented as real.
    """
    t = ticker.upper()
    meta = _DEMO_META[t]
    kpis = _kpis_for(t)
    p = kpis.provenance
    dcf_result = _dcf_model.value_from_kpis(kpis)
    _disclose_debt_effect(t, kpis, dcf_result)

    transcript = _transcripts.create_mock_transcript(t, _SYNTHETIC_QUARTERS[0])
    sentiment = _sentiment.analyze_transcript(transcript)

    report = _reporter.generate(
        ticker=t,
        kpis=kpis,
        dcf_result=dcf_result,
        comps_result=None,
        sentiment=sentiment,
        company_name=meta["company_name"],
        current_price=meta["current_price"],
        price_date=PRICE_DATE,
        # Demo mode bundles no split history, so none is checked (README, Stock splits).
        splits=[],
        sentiment_synthetic=True,
        data_sources=[
            f"Financials: SEC EDGAR XBRL — {kpis.period} 10-K, fiscal year ended "
            f"{p['fiscal_year_end']} (accession {p['accession']}), from the "
            f"{DEMO_SOURCE}",
            f"Price: ${meta['current_price']:,.2f}, {PRICE_SOURCE} {PRICE_DATE} "
            "(bundled; not a live quote)",
            *payloads.dcf_source_lines(dcf_result.assumptions),
            "Earnings sentiment: " + payloads.SYNTHETIC_TRANSCRIPT_SOURCE,
        ],
    )

    banner = (
        "> **Demo mode (`FINSIGHT_DEMO=1`) — offline.** Every figure below was "
        f"computed by the real FinSight engine from the {DEMO_SOURCE}: {t}'s "
        f"{kpis.period} 10-K (fiscal year ended {p['fiscal_year_end']}). No SEC "
        "EDGAR, Yahoo Finance or transcript request was made. The price is the "
        f"{PRICE_DATE} close, not a live quote; earnings sentiment is computed "
        "from a synthetic placeholder transcript. The comparable-company section "
        "is omitted because peer multiples require live market data and this "
        "project does not synthesise them. Unset `FINSIGHT_DEMO` for the live "
        "pipeline.\n"
    )
    return banner + "\n" + _reporter.to_markdown(report)

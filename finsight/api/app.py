"""
FinSight FastAPI application.

Endpoints:
  GET  /health                     — Health check (reports demo_mode + demo tickers)
  GET  /meta/{ticker}              — Company metadata (name, sector, current price)
  GET  /filings/{ticker}           — List recent SEC filings
  GET  /kpis/{ticker}              — Extract KPIs from latest 10-K
  GET  /dcf/{ticker}               — Run DCF valuation (with provenance)
  GET  /sentiment/{ticker}         — Sentiment analysis of latest transcript
  GET  /report/{ticker}            — Generate full research report (Markdown)

Demo mode:
  With ``FINSIGHT_DEMO=1``, every endpoint above serves bundled fixtures for the
  five demo tickers (``demo_data.DEMO_TICKERS``) and makes NO network request;
  each response is tagged ``"mode": "demo"`` (``/report`` carries a Markdown
  banner instead, being plain text). A ticker OUTSIDE that set is refused with
  a 404 explaining that demo mode is offline (``demo_data.not_bundled_detail``)
  — it never falls through to a live request. Without the flag, the live SEC
  EDGAR / Yahoo Finance path runs first and the fixtures act only as a
  fallback; a fallback response says so in ``fallback_reason``.

Live data:
  ``/kpis``, ``/dcf`` and ``/report`` read ONE fiscal year — the latest with
  reported annual revenue — through ``KPIExtractor.extract_from_companyfacts``
  and report it (``period`` / ``fiscal_year`` and ``provenance``). Live SEC
  requests need ``SEC_EDGAR_USER_AGENT`` (your name and a deliverable email);
  without one they answer 503 with instructions rather than borrow or invent an
  identity. Earnings-call transcripts are a third-party scrape, off unless
  ``FINSIGHT_LIVE_TRANSCRIPTS=1``: off, ``/sentiment`` answers ``available: false``
  and ``/report`` says so, and nothing is requested from that site.
"""

from __future__ import annotations

import math
import os
import re
import logging
from datetime import date
from typing import Any

import requests

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from finsight.ingestion.edgar import EdgarClient, SECUserAgentError, resolve_user_agent
from finsight.ingestion.market_data import MarketDataClient, no_curated_peers
from finsight.ingestion.transcripts import (
    TRANSCRIPTS_OFF_DETAIL,
    TranscriptFetcher,
    live_transcripts_enabled,
)
from finsight.extraction.kpi_extractor import KPIExtractor
from finsight.models.dcf import DCFAssumptions, DCFInputError, DCFModel
from finsight.models.comps import CompsAnalysis
from finsight.sentiment.analyzer import SentimentAnalyzer
from finsight.report.generator import ReportGenerator
from finsight.api import demo_data, payloads

logger = logging.getLogger(__name__)

app = FastAPI(
    title="FinSight API",
    description=(
        "Equity research from SEC EDGAR XBRL: cited financials, a DCF and reports. Sentiment "
        "scores earnings-call text, not filings: today a labelled synthetic placeholder."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Singletons
_edgar = EdgarClient()
_market = MarketDataClient()
_transcripts = TranscriptFetcher()
_extractor = KPIExtractor()
_dcf = DCFModel()
_comps = CompsAnalysis(_market)
_sentiment = SentimentAnalyzer()
_reporter = ReportGenerator()


def _live_kpis(ticker: str, period: str | None = None):
    """One fiscal year's KPIs from live SEC EDGAR companyfacts."""
    cik = _edgar.get_cik(ticker)
    return _extractor.extract_from_companyfacts(_edgar.get_company_facts(cik), ticker, period)


class _NoUserAgent(HTTPException):
    """No deliverable SEC identity is configured: a setup error to report, not a live
    failure for a demo fixture to cover."""


def _user_agent_unavailable(exc: SECUserAgentError) -> HTTPException:
    return _NoUserAgent(status_code=503, detail=str(exc))


def _sec_failure(t: str, exc: requests.RequestException) -> HTTPException:
    """A failure of SEC EDGAR itself, as the documented error contract.

    SEC's own 404 means it holds no such data, so 404. Any other SEC HTTP error
    or a network failure is 503: the request was fine and SEC did not answer it.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 404:
        return HTTPException(
            status_code=404,
            detail=f"SEC EDGAR holds no data for {t}: it answered HTTP 404 for this "
                   "company's filings. Foreign filers and funds may have none.",
        )
    reason = f"HTTP {status}" if status else type(exc).__name__
    return HTTPException(
        status_code=503,
        detail=f"SEC EDGAR did not answer for {t} ({reason}). Try again later.",
    )


# Ticker symbols as SEC EDGAR's ticker map writes them: letters and digits,
# with "-" (BRK-B) or "." for share classes. Anything else never reaches SEC,
# Yahoo, the transcript scraper or a cache file name.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def _ticker(raw: str) -> str:
    """The normalised ticker for a request, refusing what cannot be served.

    A malformed symbol is a 422 that does not echo it, before anything else happens. Demo mode is
    offline for every ticker: one outside the bundled set is a 404 that says
    so, never a live SEC / Yahoo / transcript request.
    """
    t = raw.strip().upper()
    if not _TICKER_RE.fullmatch(t):
        raise HTTPException(
            status_code=422,
            detail=(
                "That is not a ticker symbol: expected 1–10 letters or "
                "digits, optionally with '-' or '.' for a share class (e.g. AAPL, BRK-B)."
            ),
        )
    if demo_data.demo_enabled() and not demo_data.has_demo(t):
        raise HTTPException(status_code=404, detail=demo_data.not_bundled_detail(t))
    return t


_PERIOD = re.compile(r"(?:FY)?\d{4}|\d{4}-\d{2}-\d{2}", re.IGNORECASE)


def _real_date(period: str) -> bool:
    """False only for a ``YYYY-MM-DD`` that is no date."""
    if len(period) != 10:
        return True
    try:
        date.fromisoformat(period)
    except ValueError:
        return False
    return True


def _period(raw: str) -> str | None:
    """A ``period`` query value: ``YYYY``, ``FYYYYY`` or ``YYYY-MM-DD``; empty = latest.

    Anything else is a 422 that does NOT echo the input, and a bare ``FY`` is
    refused rather than read as "latest". So is a date no calendar has: ``2025-13-45``.
    """
    p = (raw or "").strip()
    if not p:
        return None
    if len(p) > 12 or not _PERIOD.fullmatch(p) or not _real_date(p):
        raise HTTPException(
            status_code=422,
            detail="period must be a fiscal year (YYYY or FYYYYY) or an exact "
                   "fiscal year-end (YYYY-MM-DD); omit it for the latest year.",
        )
    return p


# An SEC form type: 10-K, 8-K, 10-K/A, DEF 14A, S-1.
_FORM_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 /\-]{0,14}")


def _form_type(raw: str) -> str:
    """A ``form_type`` query value, or a 422 that does not echo it."""
    if not _FORM_TYPE.fullmatch(raw):
        raise HTTPException(
            status_code=422,
            detail="form_type must be an SEC form type: letters, digits, space, '-' or '/' "
                   "(e.g. 10-K, 8-K, 10-K/A, DEF 14A), at most 15 characters.",
        )
    return raw


# A fallback names the lookup that failed: with SEC EDGAR down, that is SEC's, not the Yahoo
# or transcript lookup that was never attempted.
_SEC_TICKER_LOOKUP = "The SEC EDGAR ticker lookup"


def _require_sec_listed(t: str) -> None:
    """Resolve ``t`` in SEC EDGAR's ticker map before any third-party request.

    Unknown → 404 and a missing/undeliverable User-Agent → 503, so an
    arbitrary string is never forwarded to Yahoo Finance. SEC failures propagate
    to the caller's own handling, which may hold a demo fallback (``_sec_failure``).
    """
    try:
        _edgar.get_cik(t)
    except SECUserAgentError as e:
        raise _user_agent_unavailable(e)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"{t} is not in SEC EDGAR's ticker map. Verify the symbol.",
        )


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "FinSight",
        "version": "0.1.0",
        "demo_mode": demo_data.demo_enabled(),
        "demo_tickers": demo_data.DEMO_TICKERS,
    }


@app.get("/meta/{ticker}")
def get_meta(ticker: str):
    """
    Lightweight company metadata (name, sector, current price).

    Live: the ticker is resolved in SEC EDGAR's ticker map first (unknown →
    404), then priced from Yahoo Finance; a failure falls back to bundled demo
    metadata for the demo tickers.
    """
    t = _ticker(ticker)
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        return {"ticker": t, "mode": "demo", **demo_data.demo_meta(t)}
    try:
        try:
            _require_sec_listed(t)
        except requests.RequestException as e:
            if demo_data.has_demo(t):
                return payloads.with_fallback_reason(
                    {"ticker": t, "mode": "demo", **demo_data.demo_meta(t)}, e,
                    what=_SEC_TICKER_LOOKUP)
            raise _sec_failure(t, e)
        profile = _market.get_company_profile(t)
        current_price = _last_close(_market.get_price_history(t, period="1d"))
        try:
            splits = _market.get_splits(t)
        except Exception as e:
            logger.warning(f"Live meta: split history failed for {t}: {e}")
            splits = None          # unknown, not "none"
        return {
            "ticker": t,
            "company_name": profile.name,
            "sector": getattr(profile, "sector", "") or "",
            "current_price": current_price,
            # Yahoo prices are split-adjusted; the dashboard compares these
            # dates with the share count's 10-K date before showing a gap.
            "splits": splits,
            "mode": "live",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Live meta failed for {t}: {e}. Falling back to demo.")
        if demo_data.has_demo(t):
            return payloads.with_fallback_reason(
                {"ticker": t, "mode": "demo", **demo_data.demo_meta(t)}, e,
                what="The live Yahoo Finance lookup")
        # The ticker resolved above, so a Yahoo outage says nothing about it: a 503, not a 404.
        raise HTTPException(
            status_code=503,
            detail=f"Yahoo Finance did not answer for {t} ({type(e).__name__}). Try again later.",
        )


@app.get("/filings/{ticker}")
def list_filings(
    ticker: str,
    form_type: str = Query("10-K", description="Filing type: 10-K, 10-Q, 8-K"),
    limit: int = Query(5, ge=1, le=20),
):
    """List recent SEC filings for a ticker."""
    t = _ticker(ticker)
    form_type = _form_type(form_type)
    # Forced demo mode: answer offline. Filing indexes are not bundled (see
    # demo_data.demo_filings for why), so this is an honest empty answer rather
    # than a live SEC request — FINSIGHT_DEMO must mean zero network here too.
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        return demo_data.demo_filings(t, form_type=form_type, limit=limit)
    try:
        cik = _edgar.get_cik(t)
        filings = _edgar.get_filings(cik, form_type=form_type, limit=limit)
        return {
            "ticker": t,
            "cik": cik,
            "form_type": form_type,
            "mode": "live",
            "count": len(filings),
            "filings": [
                {
                    "accession_number": f.accession_number,
                    "filing_date": f.filing_date,
                    "period_of_report": f.period_of_report,
                    "url": f.url,
                }
                for f in filings
            ],
        }
    except SECUserAgentError as e:
        raise _user_agent_unavailable(e)
    except requests.RequestException as e:
        logger.warning(f"SEC EDGAR failed on filings for {t}: {e}")
        raise _sec_failure(t, e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching filings for {ticker}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching filings.",
        )


@app.get("/kpis/{ticker}")
def extract_kpis(
    ticker: str,
    period: str = Query(
        "",
        description="Fiscal year (YYYY or FYYYYY). Defaults to the latest with reported annual revenue.",
    ),
):
    """KPIs for one fiscal year from the company's 10-K XBRL data.

    ``period`` in the response is the fiscal year actually used, and
    ``provenance`` names its year-end, filing and the concept behind each
    figure; anything not reported is ``null`` and listed in
    ``provenance.missing`` — never filled from another year.
    """
    t = _ticker(ticker)
    wanted = _period(period)
    # Forced demo mode: serve the bundled snapshot, skip the network entirely.
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        try:
            return demo_data.demo_kpis(t, wanted)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    try:
        return payloads.kpis_payload(_live_kpis(t, wanted), mode="live")
    except SECUserAgentError as e:
        raise _user_agent_unavailable(e)
    except requests.RequestException as e:
        logger.warning(f"SEC EDGAR failed on KPIs for {t}: {e}")
        if demo_data.has_demo(t) and wanted is None:
            return payloads.with_fallback_reason(demo_data.demo_kpis(t), e)
        raise _sec_failure(t, e)
    except ValueError as e:
        # A fixture cannot answer for a specific fiscal year it does not hold.
        if demo_data.has_demo(t) and wanted is None:
            logger.warning(f"KPI live path failed for {t}: {e}. Serving demo fixture.")
            return payloads.with_fallback_reason(demo_data.demo_kpis(t), e)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"KPI extraction failed for {ticker}: {e}")
        if demo_data.has_demo(t) and wanted is None:
            return payloads.with_fallback_reason(demo_data.demo_kpis(t), e)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while extracting KPIs.",
        )


@app.get("/dcf/{ticker}")
def run_dcf(
    ticker: str,
    wacc: float | None = Query(
        None, ge=0.05, le=0.30,
        description="Discount rate. Omitted = the model default (10%), reported as a default.",
    ),
    terminal_growth: float | None = Query(
        None, ge=0.01, le=0.05,
        description="Perpetuity growth. Omitted = the model default (3%), reported as a default.",
    ),
    n_years: int = Query(5, ge=3, le=10),
    period: str = Query(
        "",
        description="Fiscal year to value (YYYY, FYYYYY or YYYY-MM-DD). Omitted = the latest.",
    ),
):
    """DCF valuation from one fiscal year's 10-K data (the latest by default).

    ``period`` selects the base year exactly as ``/kpis`` does.

    The response carries ``mode``, the ``fiscal_year`` and ``provenance`` it
    was built from, every assumption used with its source, and the inputs that
    were model defaults — including ``wacc`` and ``terminal_growth`` when the
    caller omits them — plus every ``adjustments`` the projection makes to a
    reported figure. Missing revenue or diluted shares → 422 (no placeholder
    fundamentals); WACC − terminal growth below ``DCFModel.MIN_WACC_SPREAD``
    → 422.
    """
    t = _ticker(ticker)
    wanted = _period(period)
    used_wacc = DCFAssumptions.wacc if wacc is None else wacc
    used_g = DCFAssumptions.terminal_growth_rate if terminal_growth is None else terminal_growth
    if not DCFModel.spread_ok(used_wacc, used_g):
        raise HTTPException(
            status_code=422,
            detail=(
                f"wacc ({used_wacc:.2%}) must exceed terminal_growth "
                f"({used_g:.2%}) by at least {DCFModel.MIN_WACC_SPREAD:.0%}: "
                "the terminal value is FCF / (WACC − g), undefined at a zero "
                "spread and meaningless near one."
            ),
        )
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        try:
            return demo_data.demo_dcf(
                t, n_years=n_years, wacc=wacc, terminal_growth=terminal_growth,
                period=wanted,
            )
        except DCFInputError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    try:
        kpis = _live_kpis(t, wanted)
        result = DCFModel(n_years=n_years).value_from_kpis(
            kpis, wacc=wacc, terminal_growth=terminal_growth
        )
        return payloads.dcf_payload(t, kpis, result, n_years, mode="live")
    except SECUserAgentError as e:
        raise _user_agent_unavailable(e)
    except requests.RequestException as e:
        logger.warning(f"SEC EDGAR failed on the DCF for {t}: {e}")
        if demo_data.has_demo(t) and wanted is None:
            return payloads.with_fallback_reason(demo_data.demo_dcf(
                t, n_years=n_years, wacc=wacc, terminal_growth=terminal_growth
            ), e)
        raise _sec_failure(t, e)
    except DCFInputError as e:
        # The data is live and real, just incomplete: say so, do not paper
        # over it with a fixture or a placeholder.
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        # Lookup failure (unknown ticker/CIK, no usable financials): a clean 404,
        # never a 500. Fall back to a demo fixture first if one exists — but
        # not for a requested year: the fixture would answer a different one.
        if wanted is not None:
            raise HTTPException(status_code=404, detail=str(e))
        if demo_data.has_demo(t):
            logger.warning(f"DCF live path failed for {t}: {e}. Serving demo fixture.")
            return payloads.with_fallback_reason(demo_data.demo_dcf(
                t, n_years=n_years, wacc=wacc, terminal_growth=terminal_growth
            ), e)
        logger.info(f"DCF lookup failed for {t}: {e}")
        raise HTTPException(
            status_code=404,
            detail=f"No DCF data available for {t}: it needs a ticker in SEC EDGAR's map "
                   "with annual revenue in 10-K XBRL facts. A foreign filer on Form 20-F "
                   "or 40-F has no 10-K.",
        )
    except Exception as e:
        logger.error(f"DCF failed for {ticker}: {e}")
        if demo_data.has_demo(t) and wanted is None:
            logger.warning(f"DCF live path failed for {t}. Serving demo fixture.")
            return payloads.with_fallback_reason(demo_data.demo_dcf(
                t, n_years=n_years, wacc=wacc, terminal_growth=terminal_growth
            ), e)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while running the DCF valuation.",
        )


def _transcript_sentiments(ticker: str, n: int) -> list[tuple[Any, Any]]:
    """``(transcript, sentiment)`` for up to ``n`` recent earnings calls.

    A transcript that cannot be fetched is replaced by the synthetic
    placeholder; it carries ``synthetic=True`` and every caller reports it.
    """
    fetcher = TranscriptFetcher()
    pairs = []
    # NOTE: the loop variable must NOT be named `t` — in get_sentiment `t` is
    # the ticker string its `except` handlers pass to demo_data.has_demo().
    for tr in fetcher.search_transcripts(ticker, limit=n):
        if not getattr(tr, "synthetic", False):
            fetcher.fetch_transcript_text(tr)
        if not tr.full_text:
            tr = fetcher.create_mock_transcript(ticker, tr.quarter)
        pairs.append((tr, _sentiment.analyze_transcript(tr)))
    return pairs


@app.get("/sentiment/{ticker}")
def get_sentiment(ticker: str, n_transcripts: int = Query(1, ge=1, le=4)):
    """Analyze sentiment from recent earnings transcripts.

    Every result says whether its transcript was real (``synthetic: false``,
    ``source`` = the transcript URL) or the synthetic placeholder used when
    none could be fetched (``synthetic: true``); the top-level ``synthetic``
    is true if any result is. With ``FINSIGHT_LIVE_TRANSCRIPTS`` off, the live
    path requests nothing and answers ``available: false``, no results, and why.
    """
    t = _ticker(ticker)
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        return demo_data.demo_sentiment(t, n_transcripts=n_transcripts)
    try:
        # Resolve the ticker in SEC's map first, as /meta and /report do, so a company
        # that does not exist gets no placeholder and no request to fool.com.
        try:
            _require_sec_listed(t)
        except requests.RequestException as e:
            if demo_data.has_demo(t):
                return payloads.with_fallback_reason(
                    demo_data.demo_sentiment(t, n_transcripts=n_transcripts), e,
                    what=_SEC_TICKER_LOOKUP)
            raise _sec_failure(t, e)
        if not live_transcripts_enabled():
            return payloads.sentiment_unavailable_payload(t)
        pairs = _transcript_sentiments(t, n_transcripts)
        if not pairs:
            raise HTTPException(status_code=404, detail=f"No transcripts found for {t}")
        rows = [payloads.sentiment_row(tr, s) for tr, s in pairs]
        return payloads.sentiment_payload(t, rows, mode="live")
    except _NoUserAgent:
        raise
    except HTTPException as e:
        if demo_data.has_demo(t):
            logger.warning(f"Sentiment live path failed for {t}. Serving demo fixture.")
            return payloads.with_fallback_reason(
                demo_data.demo_sentiment(t, n_transcripts=n_transcripts), e,
                what="The live transcript lookup")
        raise
    except ValueError as e:
        if demo_data.has_demo(t):
            return payloads.with_fallback_reason(
                demo_data.demo_sentiment(t, n_transcripts=n_transcripts), e,
                what="The live transcript lookup")
        logger.info(f"Sentiment lookup failed for {t}: {e}")
        raise HTTPException(
            status_code=404,
            detail=f"No transcripts available for {t}.",
        )
    except Exception as e:
        logger.error(f"Sentiment failed for {ticker}: {e}")
        if demo_data.has_demo(t):
            return payloads.with_fallback_reason(
                demo_data.demo_sentiment(t, n_transcripts=n_transcripts), e,
                what="The live transcript lookup")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while analyzing sentiment.",
        )


def _last_close(price_history) -> float | None:
    """The last bar's close, or None without a real one. Yahoo can answer with a NaN
    close, which JSON cannot carry."""
    if price_history.empty:
        return None
    close = float(price_history["Close"].iloc[-1])
    return close if math.isfinite(close) else None


def _bar_date(price_history) -> str | None:
    """ISO date of the last bar in a price history (the price's date)."""
    try:
        return price_history.index[-1].date().isoformat()
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


@app.get("/report/{ticker}", response_class=PlainTextResponse)
def generate_report(ticker: str):
    """Generate a full equity research report in Markdown format."""
    t = _ticker(ticker)
    # Forced demo mode: run the whole report pipeline over bundled fixtures with
    # zero network access. Without this the live path below reaches SEC EDGAR and
    # Yahoo Finance even with FINSIGHT_DEMO=1.
    if demo_data.demo_enabled() and demo_data.has_demo(t):
        return demo_data.demo_report(t)
    try:
        ticker = t
        _require_sec_listed(ticker)

        sources: list[str] = []

        # Yahoo Finance (name and price) degrades like every other component, so a
        # rate limit cannot discard valid SEC sections.
        company_name = ticker
        try:
            company_name = _market.get_company_profile(ticker).name or ticker
        except Exception as e:
            logger.warning(f"Report: Yahoo profile failed for {ticker}: {e}")
            sources.append(
                f"Company name unavailable: Yahoo Finance did not answer "
                f"({type(e).__name__}); the ticker is shown instead."
            )
        current_price, price_date, splits = None, None, None
        try:
            splits = _market.get_splits(ticker)
        except Exception as e:
            logger.warning(f"Report: Yahoo split history failed for {ticker}: {e}")
            sources.append("Split history unavailable: a stock split since the share "
                           "count's 10-K could not be checked.")
        try:
            price_history = _market.get_price_history(ticker, period="1d")
            current_price = _last_close(price_history)
            if current_price is not None:
                price_date = _bar_date(price_history)
            elif not price_history.empty:
                sources.append("Price unavailable: Yahoo Finance returned no usable close, "
                               "so there is no model-vs-price comparison.")
        except Exception as e:
            logger.warning(f"Report: Yahoo price failed for {ticker}: {e}")
            sources.append(
                f"Price unavailable: Yahoo Finance did not answer ({type(e).__name__}), "
                "so there is no model-vs-price comparison."
            )

        # Each component below degrades independently: if one fails, the report
        # is still assembled from whatever succeeded (ReportGenerator.generate
        # skips None sections). We keep the broad `except` so a single component
        # can never 500 the whole report, but every degradation is now LOGGED so
        # the miss is visible in ops rather than silently swallowed.

        if current_price is not None:
            sources.append(
                f"Price: ${current_price:,.2f}, Yahoo Finance quote at report time"
            )

        # KPIs: the latest fiscal year from SEC XBRL, with its filing cited.
        try:
            kpis = _live_kpis(ticker)
            p = kpis.provenance
            sources.append(
                f"Financials: SEC EDGAR XBRL — {kpis.period} 10-K, fiscal year "
                f"ended {p['fiscal_year_end']} (accession {p['accession']})"
            )
        except SECUserAgentError as e:
            logger.warning(f"Report: {e}")
            sources.append(f"Financials omitted: {e}")
            kpis = None
        except Exception as e:
            logger.warning(f"Report: KPI extraction failed for {ticker}: {e}. "
                           "Report will omit the financial summary.")
            sources.append("Financials omitted: SEC EDGAR data could not be read.")
            kpis = None

        # DCF
        dcf_result = None
        if kpis is not None:
            try:
                dcf_result = _dcf.value_from_kpis(kpis)
                sources.extend(payloads.dcf_source_lines(dcf_result.assumptions))
            except Exception as e:
                logger.warning(f"Report: DCF valuation failed for {ticker}: {e}. "
                               "Report will omit the DCF section and model value.")
                sources.append(f"DCF omitted: {e}")

        # Comps
        comps_result, comps_unavailable = None, None
        try:
            peers = _market.get_peers(ticker)
            if peers:
                # Pass the extracted KPIs: they carry the target's EBITDA and
                # net income, which is what lets the EV/EBITDA and P/E peer
                # multiples contribute to the implied valuation at all.
                comps_result = _comps.analyze(ticker, peers=peers[:5], kpis=kpis)
            else:
                comps_unavailable = no_curated_peers(ticker)
        except Exception as e:
            logger.warning(f"Report: comps analysis failed for {ticker}: {e}. "
                           "The comparable-company section will say so.")
            comps_unavailable = f"The peer analysis failed ({type(e).__name__})."

        # Sentiment: the most recent real transcript, else the synthetic
        # placeholder — which the report then labels as synthetic.
        sentiment = None
        sentiment_synthetic = False
        sentiment_unavailable = None
        try:
            if live_transcripts_enabled():
                pairs = _transcript_sentiments(ticker, 1)
                if pairs:
                    transcript, sentiment = pairs[0]
                    sentiment_synthetic = bool(getattr(transcript, "synthetic", False))
                    sources.append(
                        "Earnings sentiment: " + (
                            payloads.SYNTHETIC_TRANSCRIPT_SOURCE if sentiment_synthetic
                            else f"{transcript.quarter} transcript, {transcript.url}"
                        )
                    )
            else:
                sentiment_unavailable = TRANSCRIPTS_OFF_DETAIL
        except Exception as e:
            logger.warning(f"Report: sentiment analysis failed for {ticker}: {e}. "
                           "Report will omit the earnings-sentiment section.")

        report = _reporter.generate(
            ticker=ticker,
            kpis=kpis,
            dcf_result=dcf_result,
            comps_result=comps_result,
            comps_unavailable=comps_unavailable,
            sentiment=sentiment,
            company_name=company_name,
            current_price=current_price,
            price_date=price_date,
            splits=splits,
            sentiment_synthetic=sentiment_synthetic,
            sentiment_unavailable=sentiment_unavailable,
            data_sources=sources,
        )

        return _reporter.to_markdown(report)

    except ValueError as e:
        logger.info(f"Report lookup failed for {ticker}: {e}")
        raise HTTPException(
            status_code=404,
            detail=f"No research data available for {t}.",
        )
    except HTTPException:
        raise
    except requests.RequestException as e:
        logger.warning(f"Report: SEC EDGAR ticker map failed for {t}: {e}")
        raise _sec_failure(t, e)
    except Exception as e:
        logger.error(f"Report generation failed for {ticker}: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while generating the report.",
        )


def main() -> None:
    """Console-script entry point: launch the FinSight REST API with uvicorn.

    Binds 127.0.0.1 without auto-reload by default — a dev server on every
    interface is not a safe default. ``FINSIGHT_HOST`` / ``FINSIGHT_PORT``
    override the bind and ``FINSIGHT_RELOAD=1`` turns reload on for
    development. Variables in a ``.env`` file in the working directory are
    loaded first (existing environment variables win).
    """
    import uvicorn
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    if not demo_data.demo_enabled():
        try:
            resolve_user_agent()
        except SECUserAgentError as e:
            logger.warning(f"Live SEC requests will fail until this is fixed: {e}")

    host = os.getenv("FINSIGHT_HOST", "127.0.0.1")
    port = int(os.getenv("FINSIGHT_PORT", "8001"))
    reload = os.getenv("FINSIGHT_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("finsight.api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()

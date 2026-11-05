"""
SEC EDGAR API client.

Fetches 10-K, 10-Q, and 8-K filings directly from the SEC's EDGAR full-text
search and data APIs. No API key required — SEC provides public access.

API endpoints:
  https://data.sec.gov/submissions/CIK{10 digits}.json — company metadata + filing list
  https://data.sec.gov/api/xbrl/companyfacts/CIK{10 digits}.json — XBRL financial data
  https://efts.sec.gov/LATEST/search-index?...     — full-text search

Fair access (https://www.sec.gov/os/accessing-edgar-data)
--------------------------------------------------------
* **Identify yourself.** Every request carries the User-Agent in
  ``SEC_EDGAR_USER_AGENT`` — *your* name and contact email. There is no
  built-in fallback identity: a live request without it — or with a contact
  that cannot receive mail, such as an ``example.com`` address — raises
  :class:`SECUserAgentError` instead of going out under a borrowed or
  invented identity.
* **Stay under 10 requests/second.** All clients in the process share one
  thread-safe limiter (:data:`SEC_RATE_LIMITER`), so the FastAPI thread pool
  cannot multiply the rate by running requests in parallel.
* **Don't re-download what you have.** The ticker→CIK map is fetched once per
  day per client (unknown tickers are answered from it, not by re-fetching),
  and companyfacts payloads are cached briefly per CIK.
"""

from __future__ import annotations

import re
import os
import threading
import time
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_EDGAR_BASE_URL = "https://www.sec.gov"

# ---------------------------------------------------------------------------
# Requester identity
# ---------------------------------------------------------------------------

USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"
# The placeholder shipped in .env.example / the docs. Sending it would identify
# nobody, so it is rejected like an unset value.
_PLACEHOLDER_CONTACT = "your-email@example.com"
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# Domains no mailbox can live at: RFC 2606 / RFC 6761 reserve these for
# documentation and testing. An identity at one of them is a costume, not a
# contact — the SEC could never reach whoever sent the traffic.
RESERVED_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_EMAIL_TLDS = ("example", "invalid", "localhost", "test")
# Addresses that exist precisely so that nobody reads what is sent to them.
_NO_REPLY_LOCAL_PARTS = ("noreply", "no-reply", "donotreply", "do-not-reply")
_NO_REPLY_DOMAINS = ("users.noreply.github.com",)


def undeliverable_contact(local: str, domain: str) -> bool:
    """True when ``local@domain`` cannot be a working contact address."""
    d = domain.lower().rstrip(".")
    reserved = d in RESERVED_EMAIL_DOMAINS or d.endswith(
        tuple("." + r for r in RESERVED_EMAIL_DOMAINS)
    )
    return (
        reserved
        or d.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS
        or d in _NO_REPLY_DOMAINS
        or local.lower() in _NO_REPLY_LOCAL_PARTS
    )


class SECUserAgentError(RuntimeError):
    """A live SEC EDGAR request was attempted without the caller's own contact."""


def _user_agent_problem(problem: str) -> str:
    return (
        f"{USER_AGENT_ENV} {problem}. SEC EDGAR's fair-access policy requires "
        "automated clients to declare who they are with a User-Agent that "
        "includes contact information "
        "(https://www.sec.gov/os/accessing-edgar-data). Set it to YOUR OWN name "
        f'and email, e.g. {USER_AGENT_ENV}="Jane Doe jane.doe@example.org". '
        "FinSight never sends live requests under anyone else's identity. "
        "Offline demo mode (FINSIGHT_DEMO=1) needs no User-Agent."
    )


def resolve_user_agent() -> str:
    """The User-Agent to send to SEC EDGAR, read from the environment.

    Read at request time — not at import — so a value set after the module is
    imported (a late-loaded .env, a test) takes effect.

    Raises
    ------
    SECUserAgentError
        If ``SEC_EDGAR_USER_AGENT`` is unset, still holds the documentation
        placeholder, carries no contact email, or carries only addresses that
        cannot receive mail (see :func:`undeliverable_contact`) — e.g. the
        ``demo@example.com`` a fallback identity would use.
    """
    configured = os.getenv(USER_AGENT_ENV, "").strip()
    if not configured:
        raise SECUserAgentError(_user_agent_problem("is not set"))
    if _PLACEHOLDER_CONTACT in configured.lower():
        raise SECUserAgentError(
            _user_agent_problem("still holds the documentation placeholder")
        )
    contacts = _EMAIL_RE.findall(configured)
    if not contacts:
        raise SECUserAgentError(_user_agent_problem("contains no contact email"))
    if all(undeliverable_contact(local, domain) for local, domain in contacts):
        domains = ", ".join(sorted({domain.lower() for _, domain in contacts}))
        raise SECUserAgentError(_user_agent_problem(
            f"has no deliverable contact email ({domains}): example.com/.net/.org, "
            "the .example/.invalid/.localhost/.test domains and no-reply "
            "addresses are reserved or unread, so they identify nobody"
        ))
    return configured


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Minimum spacing between request starts, shared by every caller.

    Thread-safe: each :meth:`acquire` reserves the next free slot under a lock
    and then sleeps (outside the lock) until that slot, so N threads calling at
    once are spaced ``min_interval`` apart rather than all firing together.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> float:
        """Block until this caller's slot; return the slot (monotonic seconds)."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)
        return slot


# 0.12 s between request starts = at most ~8.3 requests/second process-wide,
# under the SEC's documented ceiling of 10/s.
SEC_RATE_LIMITER = RateLimiter(min_interval=0.12)

# ---------------------------------------------------------------------------
# XBRL series extraction
# ---------------------------------------------------------------------------

# A fiscal-year duration fact spans a 52- or 53-week year (364 or 371 days,
# give or take a calendar quirk). Anything outside this band in a 10-K is a
# quarterly or other sub-period figure, not the year.
ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380


def _duration_days(entry: dict[str, Any]) -> int | None:
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def extract_financial_series(
    facts: dict[str, Any],
    concept: str,
    taxonomy: str = "us-gaap",
    unit: str | None = None,
    form_filter: str | None = "10-K",
    annual_only: bool = True,
) -> list[dict[str, Any]]:
    """
    Extract one XBRL concept's timeseries from a companyfacts payload.

    Parameters
    ----------
    facts : dict
        Output of :meth:`EdgarClient.get_company_facts`.
    concept : str
        XBRL concept name (e.g. 'Revenues', 'CommonStockSharesOutstanding').
    taxonomy : str
        'us-gaap' or 'dei'.
    unit : str | None
        The unit key under ``units`` ('USD', 'shares', 'USD/shares'). ``None``
        picks the concept's only unit, or 'USD' when it has several — so a
        share count is read from ``units.shares`` instead of silently coming
        back empty from ``units.USD``.
    form_filter : str | None
        Only include entries from this form type. None = all forms.
    annual_only : bool
        Keep only fiscal-year figures: duration facts must span
        ``ANNUAL_MIN_DAYS``–``ANNUAL_MAX_DAYS`` days. Instant facts (balance
        sheet, share counts) have no duration and are always kept. A 10-K also
        reports quarterly sub-period facts; without this filter a Q4 figure
        sharing the fiscal year-end date could replace the annual one.

    Returns
    -------
    list[dict]
        One entry per period end date, sorted by ``end``. Each is a copy of the
        latest-filed SEC fact for that date among those kept (with the default
        ``form_filter`` that is the form ``10-K`` exactly, so a figure a later
        10-K re-reports wins, and a 10-K/A is never read), plus
        ``unit``, ``first_filed``, ``first_fy`` and ``first_accn`` — the filing
        date, fiscal year and accession of the earliest filing that reported
        that period, which is how the period's own fiscal-year label and its
        own 10-K are recovered (a later 10-K re-reports old periods under its
        own ``fy`` and accession).
    """
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept) or {}
    units = node.get("units") or {}
    if unit is None:
        if len(units) == 1:
            unit = next(iter(units))
        elif "USD" in units:
            unit = "USD"
        elif units:
            raise ValueError(
                f"{taxonomy}:{concept} is reported in several units "
                f"({sorted(units)}); pass unit= explicitly."
            )
        else:
            return []

    entries = units.get(unit, [])
    if form_filter:
        entries = [e for e in entries if e.get("form") == form_filter]
    if annual_only:
        entries = [
            e for e in entries
            if (d := _duration_days(e)) is None or ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS
        ]

    by_end: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_end.setdefault(e.get("end", ""), []).append(e)

    series: list[dict[str, Any]] = []
    for group in by_end.values():
        latest = max(group, key=lambda e: e.get("filed", ""))
        first = min(group, key=lambda e: e.get("filed", ""))
        series.append({
            **latest,
            "unit": unit,
            "first_filed": first.get("filed"),
            "first_fy": first.get("fy"),
            "first_accn": first.get("accn"),
        })
    return sorted(series, key=lambda x: x.get("end", ""))


@dataclass
class Filing:
    """Represents a single SEC filing."""

    cik: str
    accession_number: str
    form_type: str
    filing_date: str
    period_of_report: str
    company_name: str
    primary_document: str = ""
    full_text: str = ""
    url: str = ""

    @property
    def fiscal_year(self) -> int | None:
        if self.period_of_report:
            try:
                return int(self.period_of_report[:4])
            except (ValueError, IndexError):
                return None
        return None

    def __repr__(self) -> str:
        return (
            f"Filing({self.form_type} | {self.company_name} | "
            f"{self.period_of_report} | {self.accession_number[:20]}…)"
        )


class EdgarClient:
    """
    Client for the SEC EDGAR APIs.

    Parameters
    ----------
    rate_limiter : RateLimiter | None
        Request spacing. Defaults to the process-wide :data:`SEC_RATE_LIMITER`,
        which every client shares so the SEC's 10 requests/second ceiling holds
        across threads and instances. Pass your own only for testing.

    Live requests need ``SEC_EDGAR_USER_AGENT`` (your name and email); see
    :func:`resolve_user_agent`. Constructing a client makes no request and
    needs no configuration, so offline/demo code paths can hold one.

    Examples
    --------
    >>> client = EdgarClient()
    >>> cik = client.get_cik("AAPL")
    >>> filings = client.get_filings(cik, form_type="10-K", limit=5)
    >>> facts = client.get_company_facts(cik)
    """

    TICKER_MAP_TTL_S = 24 * 3600
    FACTS_TTL_S = 600
    FACTS_CACHE_SIZE = 8

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self._limiter = rate_limiter or SEC_RATE_LIMITER
        self._session = requests.Session()
        self._session.headers.update({"Accept-Encoding": "gzip, deflate"})
        self._ticker_lock = threading.Lock()
        self._ticker_map: dict[str, str] | None = None
        self._ticker_map_fetched_at = 0.0
        self._facts_lock = threading.Lock()
        self._facts_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._facts_fetch_locks: dict[str, threading.Lock] = {}

    def get_cik(self, ticker: str) -> str:
        """
        Resolve a ticker symbol to a zero-padded 10-digit CIK.

        The SEC's ticker map is downloaded once and reused for
        ``TICKER_MAP_TTL_S`` seconds, so an unknown ticker is answered from
        the cached map instead of triggering another download.

        Raises
        ------
        ValueError
            If the ticker is not in the SEC's map.
        """
        ticker = ticker.upper().strip()
        cik = self._tickers().get(ticker)
        if cik is None:
            raise ValueError(f"CIK not found for ticker: {ticker!r}. "
                             "Verify the ticker is correct and listed on a US exchange.")
        return cik

    def _tickers(self) -> dict[str, str]:
        with self._ticker_lock:
            stale = time.monotonic() - self._ticker_map_fetched_at > self.TICKER_MAP_TTL_S
            if self._ticker_map is None or stale:
                data = self._get(_COMPANY_TICKERS_URL).json()
                self._ticker_map = {
                    str(entry["ticker"]).upper(): str(entry["cik_str"]).zfill(10)
                    for entry in data.values()
                }
                self._ticker_map_fetched_at = time.monotonic()
            return self._ticker_map

    def get_filings(
        self,
        cik: str,
        form_type: str = "10-K",
        limit: int = 10,
    ) -> list[Filing]:
        """
        Fetch a list of filings for a given company.

        Parameters
        ----------
        cik : str
            Company CIK (zero-padded to 10 digits).
        form_type : str
            Filing type: '10-K', '10-Q', '8-K', etc.
        limit : int
            Maximum number of filings to return.

        Returns
        -------
        list[Filing]
        """
        url = _SUBMISSIONS_URL.format(cik=cik)
        resp = self._get(url)
        data = resp.json()

        company_name = data.get("name", "Unknown")
        recent = data.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        periods = recent.get("reportDate", [])
        primary_docs = recent.get("primaryDocument", [])

        filings: list[Filing] = []
        for i, form in enumerate(forms):
            if form.strip().upper() == form_type.upper():
                acc = accessions[i].replace("-", "")
                filing = Filing(
                    cik=cik,
                    accession_number=accessions[i],
                    form_type=form,
                    filing_date=dates[i] if i < len(dates) else "",
                    period_of_report=periods[i] if i < len(periods) else "",
                    company_name=company_name,
                    primary_document=primary_docs[i] if i < len(primary_docs) else "",
                    url=f"{_EDGAR_BASE_URL}/Archives/edgar/data/{int(cik)}/{acc}/{primary_docs[i]}",
                )
                filings.append(filing)
                if len(filings) >= limit:
                    break

        logger.info(f"Found {len(filings)} {form_type} filings for {company_name} (CIK: {cik})")
        return filings

    def get_filing_text(self, filing: Filing, max_chars: int = 100_000) -> str:
        """
        Download the full text of a filing's primary document.

        Parameters
        ----------
        filing : Filing
            Filing object with a populated url field.
        max_chars : int
            Keep at most this many characters (default 100k). The download
            itself is capped at ``4 × max_chars`` bytes (UTF-8 is at most 4
            bytes a character) and streamed, so a huge filing is never held
            in memory whole.

        Returns
        -------
        str
            Raw text content of the filing.
        """
        if not filing.url:
            return ""
        try:
            resp = self._get(filing.url, stream=True)
            cap, chunks, size = 4 * max_chars, [], 0
            try:
                for chunk in resp.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    chunks.append(chunk[: cap - size])
                    size += len(chunks[-1])
                    if size >= cap:
                        break
            finally:
                resp.close()
            text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")[:max_chars]
            # Strip HTML tags roughly
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            filing.full_text = text
            return text
        except SECUserAgentError:
            raise
        except Exception as e:
            logger.warning(f"Could not fetch filing text for {filing}: {e}")
            return ""

    def get_company_facts(self, cik: str) -> dict[str, Any]:
        """
        Fetch XBRL company facts (structured financial data from all filings).

        Returns a nested dict: facts['us-gaap']['Revenues']['units']['USD']
        gives a list of {start, end, val, accn, fy, fp, form, filed} dicts.

        Payloads are several MB, so each CIK's is cached for ``FACTS_TTL_S``
        seconds (``FACTS_CACHE_SIZE`` CIKs at most) and concurrent requests for
        the same CIK share one download. Treat the returned dict as read-only.

        Parameters
        ----------
        cik : str
            Zero-padded CIK.

        Returns
        -------
        dict
            Full XBRL facts payload.
        """
        cached = self._cached_facts(cik)
        if cached is not None:
            return cached
        with self._facts_lock:
            fetch_lock = self._facts_fetch_locks.setdefault(cik, threading.Lock())
        with fetch_lock:
            # A concurrent request for the same CIK may have filled the cache
            # while this one waited on the lock.
            cached = self._cached_facts(cik)
            if cached is not None:
                return cached
            facts = self._get(_COMPANY_FACTS_URL.format(cik=cik)).json()
            with self._facts_lock:
                self._facts_cache[cik] = (time.monotonic(), facts)
                self._facts_cache.move_to_end(cik)
                while len(self._facts_cache) > self.FACTS_CACHE_SIZE:
                    self._facts_cache.popitem(last=False)
            return facts

    def _cached_facts(self, cik: str) -> dict[str, Any] | None:
        with self._facts_lock:
            hit = self._facts_cache.get(cik)
            if hit is None or time.monotonic() - hit[0] > self.FACTS_TTL_S:
                return None
            self._facts_cache.move_to_end(cik)
            return hit[1]

    def extract_financial_series(
        self,
        facts: dict[str, Any],
        concept: str,
        taxonomy: str = "us-gaap",
        unit: str | None = None,
        form_filter: str | None = "10-K",
        annual_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Method form of the module-level :func:`extract_financial_series`."""
        return extract_financial_series(
            facts, concept, taxonomy=taxonomy, unit=unit,
            form_filter=form_filter, annual_only=annual_only,
        )

    def search_filings(
        self,
        query: str,
        form_type: str = "10-K",
        date_from: str = "2020-01-01",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Full-text search across SEC filings.

        Parameters
        ----------
        query : str
            Search terms (e.g. 'artificial intelligence revenue growth').
        form_type : str
            Filter by form type.
        date_from : str
            Start date (YYYY-MM-DD).
        limit : int
            Max results.

        Returns
        -------
        list[dict]
        """
        params = {
            "q": f'"{query}"',
            "dateRange": "custom",
            "startdt": date_from,
            "forms": form_type,
        }
        try:
            resp = self._get(_EDGAR_SEARCH_URL, params=params)
            hits = resp.json().get("hits", {}).get("hits", [])
            return hits[:limit]
        except SECUserAgentError:
            raise
        except Exception as e:
            logger.error(f"EDGAR search failed: {e}")
            return []

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET with the caller's User-Agent, shared rate limiting and retry."""
        headers = {"User-Agent": resolve_user_agent()}
        for attempt in range(3):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, timeout=30, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.HTTPError:
                if resp.status_code == 429 and attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"SEC rate limited (429). Waiting {wait}s…")
                    time.sleep(wait)
                    continue
                raise
            except requests.ConnectionError:
                if attempt == 2:
                    raise
                time.sleep(1)
        raise RuntimeError(f"Failed to fetch {url} after 3 attempts")

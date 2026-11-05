"""
Earnings call transcript fetcher.

Data source & disclosure
------------------------
**Off by default.** Nothing here requests anything unless
``FINSIGHT_LIVE_TRANSCRIPTS=1`` is set (:func:`live_transcripts_enabled`): off,
``search_transcripts`` returns nothing and ``fetch_transcript_text`` leaves its
transcript alone, and the API answers "not available".

Turned on, ``search_transcripts`` / ``fetch_transcript_text`` scrape earnings-call
transcripts from **Motley Fool public web pages**
(``https://www.fool.com/earnings/call-transcripts/``). This is a third-party
source with its own Terms of Service. FinSight does not own or license this
content and has not reviewed whether those terms allow this: that question is
yours to answer before you turn it on. The fetcher's User-Agent names the tool
and carries no contact address; every request it makes — the listing page and each
transcript page, across all fetcher instances in the process — waits its turn on
one shared limiter (:data:`FOOL_RATE_LIMITER`, one request start per second).

**Availability.** The listing page this scraper reads
(``/earnings/call-transcripts/?ticker=…``) answered HTTP 404 when checked on
2026-11-01, so live runs currently fall back to the synthetic placeholder on
every request. A layout or URL change on a third-party site can break the
scrape at any time; FinSight treats that as "no transcript", never as data.

Only links whose URL slug or title names the requested ticker are accepted
(:func:`link_names_ticker`), so another company's call listed on the same page
cannot be scored as this company's, and only ``https`` links that stay on
fool.com (:func:`is_fool_url`) are followed or fetched.

If a scrape fails (network error, layout change, blocked request, 404), the
fetcher degrades to :meth:`TranscriptFetcher.create_mock_transcript` — a
synthetic placeholder transcript of boilerplate text, identical for every
company apart from the ticker. Every transcript it creates has
``synthetic=True`` (and an empty ``url``); the API reports that flag on every
sentiment result, so a score computed from the placeholder is never presented
as a real earnings call, and synthetic transcripts are never written to the
on-disk cache.

**On-disk cache.** With ``cache=True`` (the default) real transcripts found
for a ticker are written to ``<cache_dir>/<TICKER>_transcripts.json``
(default ``~/.finsight/transcripts``; the directory is created on the first
write, never at construction) and reused for :attr:`TranscriptFetcher.CACHE_TTL_S`
(24 h). The file name keeps only ``A–Z``, ``0–9``, ``.`` and ``-`` from the
ticker, so a ticker string cannot name a path elsewhere. A miss (a failed scrape,
or a listing with nothing for the ticker) is remembered in memory, per process and
never on disk, for :attr:`TranscriptFetcher.MISS_TTL_S` (1 h), so a page answering
404 is not requested again on every call.

In the shipped **demo mode** (``FINSIGHT_DEMO=1``) the API never calls this
scraper at all: ``finsight/api/demo_data.py`` builds sentiment from
``create_mock_transcript`` only, so demo/offline runs make **zero** requests to
fool.com. See the "Data sources & disclosure" section of the README.
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from finsight.ingestion.edgar import RateLimiter

logger = logging.getLogger(__name__)

LIVE_TRANSCRIPTS_ENV = "FINSIGHT_LIVE_TRANSCRIPTS"
TRANSCRIPTS_OFF_DETAIL = (
    "Not available: live earnings-call transcripts are off on this server "
    f"({LIVE_TRANSCRIPTS_ENV} is not set), so no transcript was requested and no tone is "
    "shown. Turning it on scrapes a third-party site, whose terms are yours to check."
)


def live_transcripts_enabled() -> bool:
    """True when the operator has opted in to the third-party scrape. Read at call
    time, so a value from a late-loaded ``.env`` takes effect."""
    return os.getenv(LIVE_TRANSCRIPTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# (cache directory, ticker) -> (when, whether the scrape failed rather than found
# nothing). The API builds a fresh fetcher per request, so this outlives a fetcher.
_MISSES: dict[tuple[str, str], tuple[float, bool]] = {}

# One request start per second to fool.com, shared by every TranscriptFetcher
# in the process (the API builds a fresh fetcher per request).
FOOL_RATE_LIMITER = RateLimiter(min_interval=1.0)


FOOL_HOSTS = frozenset({"www.fool.com", "fool.com"})
"""The only hosts the scraper will request. A listing page can link anywhere;
the scraper follows a link only if it resolves to https on one of these."""


def is_fool_url(url: str) -> bool:
    """True for an ``https://`` URL on a Motley Fool host (no other port)."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in FOOL_HOSTS
        and port in (None, 443)
        and not parsed.username and not parsed.password
    )


# Where the Q&A begins. The operator introducing the first question is the
# surest sign; a Q&A heading is the fallback, skipping one in a sentence that
# only announces it ("we will then hold a question-and-answer session").
# "[Operator instructions]" is NOT a marker: it usually appears in the opening,
# which would put almost the whole call in the "Q&A".
_FIRST_QUESTION = re.compile(
    r"(?:our|your|the) first question|first question (?:comes|is) from", re.IGNORECASE)
_QA_HEADING = re.compile(
    r"questions? and answers?|question-and-answer(?: session)?|q&a session", re.IGNORECASE)
_ANNOUNCEMENT = re.compile(r"\b(?:will|later|after|following|then)\b", re.IGNORECASE)


def qa_start(text: str) -> int:
    """Index where the Q&A section starts (``len(text)`` if none is found)."""
    m = _FIRST_QUESTION.search(text)
    if m:
        return text.rfind("\n", 0, m.start()) + 1  # start of that line
    for m in _QA_HEADING.finditer(text):
        sentence_start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        if not _ANNOUNCEMENT.search(text[sentence_start:m.start()]):
            return m.start()
    return len(text)


def link_names_ticker(href: str, title: str, ticker: str) -> bool:
    """True when a transcript link is for ``ticker``.

    Motley Fool transcript URLs end in a slug like
    ``apple-aapl-q4-2025-earnings-call-transcript`` and titles read
    ``Apple (AAPL) Q4 2025 Earnings Call Transcript``. Either naming the ticker
    is accepted; a bare substring match is not (``A`` is in every slug).
    """
    t = re.escape(ticker.lower())
    slug = urlparse(href).path.rstrip("/").rsplit("/", 1)[-1].lower()
    if re.search(rf"(?:^|-){t}-q[1-4]-\d{{4}}(?:-|$)", slug):
        return True
    return bool(re.search(rf"\((?:[A-Za-z]+:\s*)?{t}\)", title, re.IGNORECASE))


def _cache_stem(ticker: str) -> str:
    """The ticker as a safe file-name stem (no separators, no traversal)."""
    return re.sub(r"[^A-Z0-9.-]", "_", ticker.upper()).strip(".") or "_"


@dataclass
class EarningsTranscript:
    """Parsed earnings call transcript."""

    ticker: str
    company_name: str
    quarter: str      # e.g. "Q3 2023"
    date: str
    url: str
    full_text: str = ""
    prepared_remarks: str = ""
    qa_section: str = ""
    participants: list[str] = field(default_factory=list)
    synthetic: bool = False
    """True for the placeholder created by ``create_mock_transcript`` — its
    text is boilerplate, not a real earnings call."""

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    def __repr__(self) -> str:
        return f"EarningsTranscript({self.ticker} | {self.quarter} | {self.word_count} words)"


class TranscriptFetcher:
    """
    Fetches earnings call transcripts from publicly available sources.

    Parameters
    ----------
    cache : bool
        Cache fetched transcripts to disk. Default True.
    cache_dir : str
        Directory for cached transcripts.

    Examples
    --------
    With ``FINSIGHT_LIVE_TRANSCRIPTS=1``; without it this requests nothing and finds none:

    >>> fetcher = TranscriptFetcher()
    >>> transcripts = fetcher.search_transcripts("AAPL", limit=4)
    >>> for t in transcripts:
    ...     print(t.quarter, t.word_count)
    """

    _BASE_URL = "https://www.fool.com"
    _SEARCH_URL = "https://www.fool.com/earnings/call-transcripts/"
    CACHE_TTL_S = 24 * 3600
    """A cached transcript list older than this is ignored (re-scraped)."""
    MISS_TTL_S = 3600
    """A scrape that found nothing is not repeated within this long (``cache=True``)."""

    def __init__(
        self,
        cache: bool = True,
        cache_dir: str = "~/.finsight/transcripts",
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.cache = cache
        self.cache_dir = os.path.expanduser(cache_dir)
        self._limiter = rate_limiter or FOOL_RATE_LIMITER
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; FinSight/0.1; research tool)",
            "Accept": "text/html,application/xhtml+xml",
        })

    def search_transcripts(
        self,
        ticker: str,
        limit: int = 4,
    ) -> list[EarningsTranscript]:
        """
        Search for recent earnings transcripts for a ticker.

        Parameters
        ----------
        ticker : str
            Stock ticker (e.g. 'AAPL').
        limit : int
            Max transcripts to return.

        Returns
        -------
        list[EarningsTranscript]
            Empty, with no request made, unless ``FINSIGHT_LIVE_TRANSCRIPTS`` is on.
        """
        if not live_transcripts_enabled():
            logger.info(f"Not scraping transcripts for {ticker}: {LIVE_TRANSCRIPTS_ENV} is off.")
            return []
        ticker = ticker.upper()
        cache_file = (
            os.path.join(self.cache_dir, f"{_cache_stem(ticker)}_transcripts.json")
            if self.cache else None
        )

        if cache_file:
            fresh = (
                os.path.exists(cache_file)
                and time.time() - os.path.getmtime(cache_file) <= self.CACHE_TTL_S
            )
            if fresh:
                with open(cache_file) as f:
                    cached = json.load(f)
                logger.info(f"Loaded {len(cached)} cached transcripts for {ticker}")
                # A placeholder has no URL. Caches written before the synthetic
                # flag existed could hold one, so a URL-less entry is treated
                # as synthetic rather than trusted as a real call.
                loaded = []
                for entry in cached[:limit]:
                    tr = EarningsTranscript(**entry)
                    tr.synthetic = tr.synthetic or not tr.url
                    loaded.append(tr)
                return loaded
            miss = _MISSES.get((self.cache_dir, ticker))
            if miss and time.time() - miss[0] <= self.MISS_TTL_S:
                logger.info(f"Not asking fool.com about {ticker} again: it found nothing recently.")
                return self._placeholders(ticker, limit) if miss[1] else []

        transcripts = self._scrape_fool_transcripts(ticker, limit)

        real = [t for t in transcripts if not t.synthetic]
        if cache_file and real:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump([t.__dict__ for t in real], f, indent=2)
            _MISSES.pop((self.cache_dir, ticker), None)
        elif cache_file:
            # The scrape fails into placeholders, and a listing with no link is empty.
            _MISSES[(self.cache_dir, ticker)] = (time.time(), bool(transcripts))

        return transcripts

    def fetch_transcript_text(self, transcript: EarningsTranscript) -> EarningsTranscript:
        """
        Fetch and parse the full text of a transcript.

        Populates transcript.full_text, prepared_remarks, qa_section. A URL
        that is not ``https`` on a fool.com host (:func:`is_fool_url`) is
        never requested — the transcript is returned untouched.

        Parameters
        ----------
        transcript : EarningsTranscript
            Transcript with populated url field.

        Returns
        -------
        EarningsTranscript (modified in-place)
        """
        if not live_transcripts_enabled():
            logger.info(f"Not fetching {transcript.url!r}: {LIVE_TRANSCRIPTS_ENV} is off.")
            return transcript
        if not is_fool_url(transcript.url):
            logger.warning(f"Not fetching {transcript.url!r}: not an https fool.com URL.")
            return transcript
        try:
            resp = self._get_on_fool(transcript.url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract article body
            article = soup.find("article") or soup.find("div", class_=re.compile("article"))
            if article is None:
                full_text = soup.get_text(separator="\n", strip=True)
            else:
                full_text = article.get_text(separator="\n", strip=True)

            transcript.full_text = full_text[:50_000]  # cap at 50k chars

            # Split prepared remarks vs Q&A (see qa_start)
            cut = qa_start(transcript.full_text)
            transcript.prepared_remarks = transcript.full_text[:cut].strip()
            transcript.qa_section = transcript.full_text[cut:].strip()

            # Extract participants
            transcript.participants = self._extract_participants(full_text)

        except Exception as e:
            logger.warning(f"Could not fetch transcript for {transcript}: {e}")

        return transcript

    def create_mock_transcript(self, ticker: str, quarter: str = "Q4") -> EarningsTranscript:
        """
        Create a synthetic placeholder transcript (no network access).

        The text is generic boilerplate — no figures, the same for every
        company, and it says so in its first line — so any sentiment computed
        from it is illustrative only. The returned
        transcript has ``synthetic=True``, no date and no URL.

        Parameters
        ----------
        ticker : str
            Ticker symbol.
        quarter : str
            Quarter string.

        Returns
        -------
        EarningsTranscript with synthetic content (``synthetic=True``).
        """
        # Generic on purpose: no figures, no products, no segments — nothing a
        # reader could take for something this company said.
        # One sample positive and one sample negative sentence keep it
        # neutral (net 0) so it never leans either way.
        mock_text = f"""
{ticker} {quarter}: SYNTHETIC PLACEHOLDER, NOT AN EARNINGS CALL

This text stands in for an earnings-call transcript that was not available. No company said any of it. It contains no figures and reads the same for every ticker.

Speaker A: Sample remark: results were strong and margins improved.
Speaker A: Sample remark: demand was weak and costs created pressure.
Speaker B: Sample remark: the period ended on its scheduled date.

Questions and Answers:
Analyst: This is a sample question.
Speaker A: This is a sample answer.
"""
        return EarningsTranscript(
            ticker=ticker,
            company_name=ticker,
            quarter=quarter,
            date="",
            url="",
            full_text=mock_text,
            prepared_remarks=mock_text.split("Questions and Answers:")[0],
            qa_section=mock_text.split("Questions and Answers:")[1] if "Questions and Answers:" in mock_text else "",
            synthetic=True,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _scrape_fool_transcripts(self, ticker: str, limit: int) -> list[EarningsTranscript]:
        """Scrape the Motley Fool transcript listing for ``ticker``'s calls."""
        url = f"{self._BASE_URL}/earnings/call-transcripts/?ticker={ticker}"
        try:
            resp = self._get_on_fool(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            results = []
            seen: set[str] = set()
            for link in soup.find_all("a", href=re.compile(r"/earnings/call-transcripts/")):
                href = link.get("href", "")
                if not href or href == "/earnings/call-transcripts/":
                    continue
                title = link.get_text(strip=True)
                if not link_names_ticker(href, title, ticker):
                    continue  # another company's call on the same listing
                # Resolve relative links against fool.com, then refuse anything
                # that left it: an absolute or protocol-relative ("//host/…")
                # link to another host, or plain http.
                full_url = urljoin(self._BASE_URL + "/", href)
                if not is_fool_url(full_url):
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)
                quarter = self._parse_quarter_from_title(title)
                t = EarningsTranscript(
                    ticker=ticker,
                    company_name=ticker,
                    quarter=quarter or title[:30],
                    date="",
                    url=full_url,
                )
                results.append(t)
                if len(results) >= limit:
                    break

            return results
        except Exception as e:
            logger.warning(f"Transcript scrape failed for {ticker}: {e}. "
                           "Using synthetic placeholder transcripts (flagged synthetic).")
            return self._placeholders(ticker, limit)

    def _placeholders(self, ticker: str, limit: int) -> list[EarningsTranscript]:
        return [self.create_mock_transcript(ticker, f"Q{q} (synthetic)")
                for q in [4, 3, 2, 1]][:limit]

    MAX_REDIRECTS = 5

    def _get_on_fool(self, url: str):
        """GET ``url``, following redirects ONLY while each hop stays on fool.com.

        Redirects are never followed automatically (``allow_redirects=False``):
        each ``Location`` is resolved and checked with :func:`is_fool_url`
        before it is requested, and each hop waits its turn on the shared
        limiter, so a redirect off fool.com is not followed.
        """
        for _ in range(self.MAX_REDIRECTS + 1):
            self._limiter.acquire()
            resp = self._session.get(url, timeout=20, allow_redirects=False)
            if getattr(resp, "status_code", 200) not in (301, 302, 303, 307, 308):
                return resp
            target = urljoin(url, (getattr(resp, "headers", None) or {}).get("Location", ""))
            if not is_fool_url(target):
                raise ValueError(f"redirect from {url!r} to {target!r} leaves fool.com; not followed")
            url = target
        raise ValueError(f"more than {self.MAX_REDIRECTS} redirects from fool.com; stopped")

    def _parse_quarter_from_title(self, title: str) -> str | None:
        match = re.search(r"(Q[1-4]\s+\d{4})", title, re.IGNORECASE)
        return match.group(1) if match else None

    def _extract_participants(self, text: str) -> list[str]:
        """Extract speaker names from transcript."""
        pattern = re.compile(r"^([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3})\s*[\-–]", re.MULTILINE)
        matches = pattern.findall(text)
        return list(dict.fromkeys(matches))[:20]  # unique, max 20

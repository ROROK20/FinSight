"""
Synthetic sentiment must never pass as a real earnings call.

Every sentiment result states whether its transcript was real, and the report labels a
synthetic section. All offline: fetchers are stubbed or have their HTTP session failed the way
a real outage would.
"""

import importlib

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

from finsight.ingestion.edgar import RateLimiter
from finsight.ingestion.transcripts import EarningsTranscript, TranscriptFetcher

REAL_URL = "https://www.fool.com/earnings/call-transcripts/2025/10/30/apple-aapl-q4-2025/"


@pytest.fixture(autouse=True)
def _scraper_turned_on(monkeypatch):
    """This file tests what the scraper does once the operator has opted in; what
    happens without that is in tests/test_transcript_switch.py."""
    monkeypatch.setenv("FINSIGHT_LIVE_TRANSCRIPTS", "1")


def _real_transcript():
    return EarningsTranscript(
        ticker="AAPL", company_name="Apple Inc.", quarter="Q4 2025",
        date="2025-10-30", url=REAL_URL,
        full_text=("Revenue declined and margins fell sharply. We lowered our "
                   "guidance amid weak demand, losses and impairment charges."),
    )


@pytest.fixture()
def live_app(monkeypatch, tmp_path):
    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # transcript cache under a temp dir
    import finsight.api.demo_data as demo_data
    import finsight.api.app as app_module

    importlib.reload(demo_data)
    importlib.reload(app_module)
    assert not demo_data.demo_enabled()

    class _Profile:
        name = "Apple Inc."
        sector = "Technology"

    monkeypatch.setattr(app_module._market, "get_company_profile", lambda t: _Profile())
    monkeypatch.setattr(app_module._market, "get_price_history",
                        lambda t, period="1d": pd.DataFrame({"Close": [250.0]}))
    monkeypatch.setattr(app_module._market, "get_peers", lambda t: [])

    # AAPL resolves in the ticker map (the report checks that before anything
    # else); its companyfacts are unavailable, so financials are omitted.
    def _no_sec(*a, **k):
        raise RuntimeError("SEC companyfacts unavailable in this test")

    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000320193")
    monkeypatch.setattr(app_module._edgar, "get_company_facts", _no_sec)
    return app_module


def _offline_fetcher_cls():
    class _OfflineFetcher(TranscriptFetcher):
        def __init__(self):
            super().__init__(cache=False, rate_limiter=RateLimiter(0))

            def _down(*a, **k):
                raise requests.ConnectionError("network down")

            self._session.get = _down

    return _OfflineFetcher


def _real_fetcher_cls():
    class _RealFetcher:
        def search_transcripts(self, ticker, limit=1):
            return [_real_transcript()]

        def fetch_transcript_text(self, tr):
            return tr

        def create_mock_transcript(self, ticker, quarter):
            raise AssertionError("a real transcript was available")

    return _RealFetcher


def test_live_sentiment_falling_back_to_the_placeholder_says_so(live_app, monkeypatch):
    monkeypatch.setattr(live_app, "TranscriptFetcher", _offline_fetcher_cls())
    body = TestClient(live_app.app).get("/sentiment/AAPL?n_transcripts=2").json()
    assert body["mode"] == "live"
    assert body["synthetic"] is True
    assert body["detail"].startswith("No real earnings-call transcript")
    assert len(body["results"]) == 2
    assert all(r["synthetic"] for r in body["results"])
    assert all("synthetic placeholder" in r["source"] for r in body["results"])


def test_live_sentiment_from_a_real_transcript_cites_it(live_app, monkeypatch):
    monkeypatch.setattr(live_app, "TranscriptFetcher", _real_fetcher_cls())
    body = TestClient(live_app.app).get("/sentiment/AAPL").json()
    assert body["synthetic"] is False
    assert "detail" not in body
    (row,) = body["results"]
    assert row["synthetic"] is False
    assert row["source"] == REAL_URL
    assert row["guidance_tone"] == "lowered"


def test_live_report_analyses_the_real_transcript_when_there_is_one(live_app, monkeypatch):
    """/report used create_mock_transcript unconditionally."""
    monkeypatch.setattr(live_app, "TranscriptFetcher", _real_fetcher_cls())
    text = TestClient(live_app.app).get("/report/AAPL").text
    assert "## Earnings Sentiment Analysis" in text
    assert "Synthetic transcript" not in text
    assert REAL_URL in text
    assert "**Guidance Tone:** Lowered" in text


def test_live_report_labels_a_placeholder_transcript(live_app, monkeypatch):
    monkeypatch.setattr(live_app, "TranscriptFetcher", _offline_fetcher_cls())
    text = TestClient(live_app.app).get("/report/AAPL").text
    assert "Synthetic transcript." in text
    assert "synthetic placeholder transcript" in text  # Data Sources line


def test_demo_sentiment_is_tagged_synthetic(monkeypatch):
    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    import finsight.api.demo_data as demo_data

    importlib.reload(demo_data)
    body = demo_data.demo_sentiment("NVDA", n_transcripts=2)
    assert body["mode"] == "demo" and body["synthetic"] is True
    assert all(r["synthetic"] and "(synthetic)" in r["quarter"] for r in body["results"])
    assert "Synthetic transcript." in demo_data.demo_report("NVDA")


class TestTranscriptCache:
    def test_placeholder_transcripts_are_flagged_and_never_cached(self, tmp_path):
        fetcher = TranscriptFetcher(cache=True, cache_dir=str(tmp_path), rate_limiter=RateLimiter(0))

        def _down(*a, **k):
            raise requests.ConnectionError("network down")

        fetcher._session.get = _down
        transcripts = fetcher.search_transcripts("AAPL", limit=2)
        assert transcripts and all(t.synthetic for t in transcripts)
        assert not list(tmp_path.iterdir()), "a synthetic transcript was cached"

    def test_url_less_entries_in_an_old_cache_load_as_synthetic(self, tmp_path):
        import json

        legacy = TranscriptFetcher(cache=False).create_mock_transcript("AAPL", "Q4 2023").__dict__
        legacy.pop("synthetic")  # caches written before the flag existed
        (tmp_path / "AAPL_transcripts.json").write_text(json.dumps([legacy]))
        (loaded,) = TranscriptFetcher(cache=True, cache_dir=str(tmp_path)).search_transcripts("AAPL", 1)
        assert loaded.synthetic is True

    def test_the_cache_holds_links_not_transcript_text(self, tmp_path, monkeypatch):
        """The scrape returns links and the text is fetched afterwards, so the cache
        holds links, as SECURITY.md says."""
        import json
        from pathlib import Path

        self._real_scrape(monkeypatch, [])
        TranscriptFetcher(cache=True, cache_dir=str(tmp_path)).search_transcripts("AAPL", 1)
        (entry,) = json.loads((tmp_path / "AAPL_transcripts.json").read_text())
        assert entry["url"] == REAL_URL
        assert entry["full_text"] == entry["prepared_remarks"] == entry["qa_section"] == ""
        security = " ".join((Path(__file__).parents[1] / "SECURITY.md").read_text(encoding="utf-8").split())
        assert "cache the links to the real earnings-call transcripts" in security
        assert "cache the real earnings-call transcripts" not in security

    def _real_scrape(self, monkeypatch, calls):
        def _scrape(self, ticker, limit):
            calls.append(ticker)
            return [EarningsTranscript(ticker=ticker, company_name=ticker, quarter="Q4 2025",
                                       date="", url=REAL_URL)]
        monkeypatch.setattr(TranscriptFetcher, "_scrape_fool_transcripts", _scrape)

    def test_a_ticker_cannot_name_a_cache_file_outside_the_cache_dir(self, tmp_path, monkeypatch):
        """The cache path keeps only safe characters, so a ticker like "../X" (or backslashes on Windows) cannot write elsewhere."""
        cache_dir = tmp_path / "a" / "b"
        self._real_scrape(monkeypatch, [])
        TranscriptFetcher(cache=True, cache_dir=str(cache_dir)).search_transcripts("../X", 1)
        assert not list((tmp_path / "a").glob("*.json")), "cache escaped its directory"
        assert [p.name for p in cache_dir.iterdir()] == ["_X_transcripts.json"]
        TranscriptFetcher(cache=True, cache_dir=str(cache_dir)).search_transcripts("..\\Y", 1)
        assert sorted(p.name for p in cache_dir.iterdir()) == ["_X_transcripts.json", "_Y_transcripts.json"]

    def test_constructing_a_fetcher_creates_no_directory(self, tmp_path, monkeypatch):
        """The API builds fetchers at import, so ~/.finsight/transcripts
        appeared on disk just by importing finsight.api.app — in demo mode too."""
        TranscriptFetcher(cache=True, cache_dir=str(tmp_path / "never"))
        assert not (tmp_path / "never").exists()
        monkeypatch.setenv("HOME", str(tmp_path))
        import finsight.api.app as app_module
        importlib.reload(app_module)
        assert not (tmp_path / ".finsight").exists()

    def test_a_stale_cache_is_scraped_again(self, tmp_path, monkeypatch):
        """The cache never expired, so a ticker's newest call never appeared."""
        import json
        import os
        import time

        stale = _real_transcript().__dict__
        path = tmp_path / "AAPL_transcripts.json"
        path.write_text(json.dumps([stale]))
        old = time.time() - TranscriptFetcher.CACHE_TTL_S - 60
        os.utime(path, (old, old))
        calls = []
        self._real_scrape(monkeypatch, calls)
        TranscriptFetcher(cache=True, cache_dir=str(tmp_path)).search_transcripts("AAPL", 1)
        assert calls == ["AAPL"]
        calls.clear()
        TranscriptFetcher(cache=True, cache_dir=str(tmp_path)).search_transcripts("AAPL", 1)
        assert calls == [], "a fresh cache was not used"


class _Page:
    def __init__(self, html):
        self.text = html
        self.status_code = 200

    def raise_for_status(self):
        pass


_LISTING = """
<a href="/earnings/call-transcripts/2025/10/30/microsoft-msft-q1-2026-earnings-call-transcript/">
  Microsoft (MSFT) Q1 2026 Earnings Call Transcript</a>
<a href="/earnings/call-transcripts/2025/10/30/apple-aapl-q4-2025-earnings-call-transcript/">
  Apple (AAPL) Q4 2025 Earnings Call Transcript</a>
<a href="/earnings/call-transcripts/2025/10/30/apple-aapl-q4-2025-earnings-call-transcript/">
  <img alt=""></a>
<a href="/earnings/call-transcripts/2025/07/31/a-o-smith-aos-q2-2025-earnings-call-transcript/">
  A. O. Smith (AOS) Q2 2025 Earnings Call Transcript</a>
"""


class TestScraperOnlyTakesTheRequestedCompany:
    """Only a link naming the ticker is accepted: another company's call on the listing must not be scored as this one's."""

    def _fetcher(self, html, limiter=None):
        fetcher = TranscriptFetcher(cache=False, rate_limiter=limiter or RateLimiter(0))
        fetcher._session.get = lambda url, **kw: _Page(html)
        return fetcher

    def test_another_companys_call_is_never_returned(self):
        found = self._fetcher(_LISTING).search_transcripts("AAPL", limit=4)
        assert [t.url for t in found] == [
            "https://www.fool.com/earnings/call-transcripts/2025/10/30/"
            "apple-aapl-q4-2025-earnings-call-transcript/"]
        assert found[0].quarter == "Q4 2025" and not found[0].synthetic

    def test_a_listing_without_the_ticker_yields_nothing(self):
        assert self._fetcher(_LISTING).search_transcripts("NVDA", limit=4) == []

    @pytest.mark.parametrize("href,title,ticker,expected", [
        ("/earnings/call-transcripts/2025/10/30/apple-aapl-q4-2025-earnings-call-transcript/", "", "AAPL", True),
        ("/earnings/call-transcripts/2025/10/30/some-slug/", "Apple (NASDAQ: AAPL) Q4 2025", "AAPL", True),
        ("/earnings/call-transcripts/2025/07/31/a-o-smith-aos-q2-2025-earnings-call-transcript/",
         "A. O. Smith (AOS) Q2 2025 Earnings Call Transcript", "A", False),
        ("/earnings/call-transcripts/2025/07/31/a-o-smith-aos-q2-2025-earnings-call-transcript/", "", "AOS", True),
        ("/earnings/call-transcripts/2025/10/30/microsoft-msft-q1-2026-earnings-call-transcript/",
         "Microsoft (MSFT) Q1 2026", "AAPL", False),
    ])
    def test_link_names_ticker(self, href, title, ticker, expected):
        from finsight.ingestion.transcripts import link_names_ticker
        assert link_names_ticker(href, title, ticker) is expected


class TestScraperRateLimit:
    """The fetcher rate-limits itself: every request, listing and transcript page, waits its turn."""

    def test_every_request_waits_its_turn(self):
        class _Counting(RateLimiter):
            def __init__(self):
                super().__init__(0)
                self.n = 0

            def acquire(self):
                self.n += 1
                return super().acquire()

        limiter = _Counting()
        gets = []
        fetcher = TranscriptFetcher(cache=False, rate_limiter=limiter)
        fetcher._session.get = lambda url, **kw: gets.append(url) or _Page(_LISTING)
        for tr in fetcher.search_transcripts("AAPL", limit=4):
            fetcher.fetch_transcript_text(tr)
        assert len(gets) == 2 and limiter.n == 2

    def test_fetchers_share_one_limiter_by_default(self):
        from finsight.ingestion.transcripts import FOOL_RATE_LIMITER
        assert TranscriptFetcher(cache=False)._limiter is FOOL_RATE_LIMITER
        assert FOOL_RATE_LIMITER.min_interval >= 1.0


_OFF_HOST_LISTING = """
<a href="https://evil.example/earnings/call-transcripts/2025/10/30/apple-aapl-q4-2025-earnings-call-transcript/">
  Apple (AAPL) Q4 2025 Earnings Call Transcript</a>
<a href="//evil.example/earnings/call-transcripts/2025/07/31/apple-aapl-q3-2025-earnings-call-transcript/">
  Apple (AAPL) Q3 2025 Earnings Call Transcript</a>
<a href="http://www.fool.com/earnings/call-transcripts/2025/05/01/apple-aapl-q2-2025-earnings-call-transcript/">
  Apple (AAPL) Q2 2025 Earnings Call Transcript</a>
<a href="https://www.fool.com.evil.example/earnings/call-transcripts/2025/01/30/apple-aapl-q1-2025-earnings-call-transcript/">
  Apple (AAPL) Q1 2025 Earnings Call Transcript</a>
<a href="/earnings/call-transcripts/2024/10/31/apple-aapl-q4-2024-earnings-call-transcript/">
  Apple (AAPL) Q4 2024 Earnings Call Transcript</a>
"""


class TestScraperStaysOnFool:
    """An absolute link on the listing page to another host is not followed."""

    def test_off_host_and_plain_http_links_are_not_followed(self):
        fetcher = TranscriptFetcher(cache=False, rate_limiter=RateLimiter(0))
        fetcher._session.get = lambda url, **kw: _Page(_OFF_HOST_LISTING)
        found = fetcher.search_transcripts("AAPL", limit=4)
        assert [t.url for t in found] == [
            "https://www.fool.com/earnings/call-transcripts/2024/10/31/"
            "apple-aapl-q4-2024-earnings-call-transcript/"]

    def test_an_off_host_transcript_url_is_never_requested(self):
        from finsight.ingestion.transcripts import EarningsTranscript

        gets = []
        fetcher = TranscriptFetcher(cache=False, rate_limiter=RateLimiter(0))
        fetcher._session.get = lambda url, **kw: gets.append(url) or _Page("<article>x</article>")
        for url in ("https://evil.example/earnings/call-transcripts/x/",
                    "http://www.fool.com/earnings/call-transcripts/x/",
                    "https://www.fool.com:8443/earnings/call-transcripts/x/"):
            t = EarningsTranscript(ticker="AAPL", company_name="AAPL", quarter="Q4 2025",
                                   date="", url=url)
            assert fetcher.fetch_transcript_text(t).full_text == ""
        assert gets == []
        ok = EarningsTranscript(ticker="AAPL", company_name="AAPL", quarter="Q4 2025", date="",
                                url="https://www.fool.com/earnings/call-transcripts/x/")
        assert fetcher.fetch_transcript_text(ok).full_text == "x"
        assert gets == ["https://www.fool.com/earnings/call-transcripts/x/"]


# ---------------------------------------------------------------------------
# Q&A split, drift without a Q&A, risk plurals
# ---------------------------------------------------------------------------

_CALL = """Operator: Good day and welcome. [Operator instructions] Please note this call is recorded.
CEO: Thank you. Results were strong and margins improved.
CFO: Demand was steady.
Operator: Our first question comes from Jane Doe with Example Securities.
Jane Doe: Can you talk about weak demand in Europe?
CEO: Demand there was weak and pricing pressure was a concern."""


class TestQASplit:
    def test_operator_instructions_in_the_opening_do_not_start_the_qa(self):
        from finsight.ingestion.transcripts import qa_start
        cut = qa_start(_CALL)
        assert _CALL[cut:].startswith("Operator: Our first question comes from")
        assert "Results were strong" in _CALL[:cut]

    def test_an_announced_qa_session_is_not_its_start(self):
        from finsight.ingestion.transcripts import qa_start
        text = ("CEO: After my remarks we will hold a question-and-answer session. Sales were up.\n"
                "Question-and-Answer Session\nAnalyst: What about margins?")
        assert text[qa_start(text):].startswith("Question-and-Answer Session")

    def test_no_marker_means_no_qa(self):
        from finsight.ingestion.transcripts import qa_start
        assert qa_start("CEO: Results were strong.") == len("CEO: Results were strong.")

    def test_fetched_transcript_is_split_at_the_first_question(self):
        from finsight.ingestion.transcripts import EarningsTranscript

        fetcher = TranscriptFetcher(cache=False, rate_limiter=RateLimiter(0))
        fetcher._session.get = lambda url, **kw: _Page(f"<article>{_CALL}</article>")
        t = fetcher.fetch_transcript_text(EarningsTranscript(
            ticker="AAPL", company_name="AAPL", quarter="Q4 2025", date="",
            url="https://www.fool.com/earnings/call-transcripts/x/"))
        assert t.qa_section.startswith("Operator: Our first question")
        assert "[Operator instructions]" in t.prepared_remarks


def test_no_qa_section_means_no_drift():
    """With no Q&A there is no drift: 0 − (remarks score) would be a made-up number."""
    from types import SimpleNamespace
    from finsight.api.payloads import sentiment_row
    from finsight.sentiment.analyzer import SentimentAnalyzer

    t = SimpleNamespace(ticker="X", quarter="Q1", full_text="Results were strong.",
                        prepared_remarks="Results were strong.", qa_section="",
                        synthetic=False, url="https://www.fool.com/x/")
    result = SentimentAnalyzer().analyze_transcript(t)
    assert result.sentiment_drift is None
    assert sentiment_row(t, result)["sentiment_drift"] is None


def test_risk_mentions_count_plurals():
    from types import SimpleNamespace
    from finsight.sentiment.analyzer import SentimentAnalyzer

    text = "Risks, headwinds, challenges and pressures persist. One risk and one uncertainty remain."
    t = SimpleNamespace(ticker="X", quarter="Q1", full_text=text, prepared_remarks=text, qa_section="")
    assert SentimentAnalyzer().analyze_transcript(t).risk_mentions == 6


def test_the_placeholder_is_generic_and_says_so():
    """The placeholder holds no figure, product or segment a reader could take for something this company said."""
    import re
    t = TranscriptFetcher(cache=False).create_mock_transcript("TSLA", "Q4 (synthetic)")
    assert t.full_text.strip().startswith("TSLA Q4 (synthetic): SYNTHETIC PLACEHOLDER, NOT AN EARNINGS CALL")
    assert "No company said any of it" in t.full_text
    assert not re.search(r"\d", t.full_text.replace("Q4", "")), "no figures"
    for specific in ("margin expanded", "cloud", "AI", "subscribers", "buybacks", "EPS"):
        assert specific not in t.full_text
    assert t.qa_section.strip().startswith("Analyst: This is a sample question.")


class _Redirect:
    def __init__(self, location, code=302):
        self.status_code, self.headers, self.text = code, {"Location": location}, ""

    def raise_for_status(self):
        pass


class TestRedirectsStayOnFool:
    """Every redirect hop is checked: requests' automatic handling would follow one off fool.com."""

    def _fetcher(self, responses):
        gets = []
        fetcher = TranscriptFetcher(cache=False, rate_limiter=RateLimiter(0))

        def get(url, **kw):
            gets.append((url, kw.get("allow_redirects")))
            return responses[len(gets) - 1]
        fetcher._session.get = get
        return fetcher, gets

    def _transcript(self):
        from finsight.ingestion.transcripts import EarningsTranscript
        return EarningsTranscript(ticker="AAPL", company_name="AAPL", quarter="Q4 2025", date="",
                                  url="https://www.fool.com/earnings/call-transcripts/x/")

    def test_a_redirect_off_fool_is_not_followed(self):
        fetcher, gets = self._fetcher([_Redirect("https://evil.example/steal")])
        assert fetcher.fetch_transcript_text(self._transcript()).full_text == ""
        assert gets == [("https://www.fool.com/earnings/call-transcripts/x/", False)]

    def test_a_redirect_to_plain_http_is_not_followed(self):
        fetcher, gets = self._fetcher([_Redirect("http://www.fool.com/earnings/x/")])
        assert fetcher.fetch_transcript_text(self._transcript()).full_text == ""
        assert len(gets) == 1

    def test_a_redirect_within_fool_is_followed_hop_by_hop(self):
        fetcher, gets = self._fetcher([_Redirect("/earnings/call-transcripts/y/", 301),
                                       _Page("<article>moved</article>")])
        assert fetcher.fetch_transcript_text(self._transcript()).full_text == "moved"
        assert gets == [("https://www.fool.com/earnings/call-transcripts/x/", False),
                        ("https://www.fool.com/earnings/call-transcripts/y/", False)]

    def test_the_listing_request_does_not_follow_an_off_host_redirect(self):
        fetcher, gets = self._fetcher([_Redirect("https://evil.example/")])
        found = fetcher.search_transcripts("AAPL", limit=1)
        assert all(t.synthetic for t in found) and len(gets) == 1


def test_sentiment_for_an_unknown_ticker_is_a_404_before_any_request(monkeypatch):
    """/sentiment/ZZZZ is refused without the placeholder or a request to fool.com: it checks SEC's ticker map first."""
    import importlib
    import finsight.api.app as app_module
    from starlette.testclient import TestClient

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    sent = []

    def _unknown(t):
        raise ValueError(f"{t} not in the ticker map")
    monkeypatch.setattr(app_module._edgar, "get_cik", _unknown)
    monkeypatch.setattr(app_module, "_transcript_sentiments", lambda t, n: sent.append(t) or [])
    r = TestClient(app_module.app).get("/sentiment/ZZZZ")
    assert r.status_code == 404 and "not in SEC EDGAR's ticker map" in r.json()["detail"]
    assert sent == []

"""The transcript scrape is a third-party site, so it is opt-in: without the switch no
/sentiment or /report call requests fool.com, and a page answering 404 is not asked again on
every search.
"""

import pytest
from fastapi.testclient import TestClient

from finsight.ingestion import transcripts
from finsight.ingestion.edgar import RateLimiter
from finsight.ingestion.transcripts import EarningsTranscript, TranscriptFetcher, live_transcripts_enabled


class _Session:
    """Records every request; answers as the listing does, a 404 by default."""

    def __init__(self, status=404, text=""):
        self.urls = []
        self.status, self.text = status, text
        self.headers = {}

    def get(self, url, **kwargs):
        self.urls.append(url)
        session = self

        class _Response:
            status_code = session.status
            text = session.text
            headers = {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

        return _Response()


def _fetcher(tmp_path, session, cache=True):
    fetcher = TranscriptFetcher(cache=cache, cache_dir=str(tmp_path), rate_limiter=RateLimiter(0))
    fetcher._session = session
    return fetcher


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("FINSIGHT_LIVE_TRANSCRIPTS", raising=False)


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("FINSIGHT_LIVE_TRANSCRIPTS", "1")


# --- the switch ------------------------------------------------------------------

@pytest.mark.parametrize("value, enabled", [(None, False), ("", False), ("0", False), ("no", False),
                                            ("1", True), ("true", True), ("ON", True)])
def test_the_flag_defaults_off(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("FINSIGHT_LIVE_TRANSCRIPTS", raising=False)
    else:
        monkeypatch.setenv("FINSIGHT_LIVE_TRANSCRIPTS", value)
    assert live_transcripts_enabled() is enabled


def test_off_the_fetcher_requests_nothing(off, tmp_path):
    session = _Session()
    fetcher = _fetcher(tmp_path, session)
    assert fetcher.search_transcripts("AAPL", limit=4) == []
    tr = EarningsTranscript(ticker="AAPL", company_name="AAPL", quarter="Q4 2025", date="",
                            url="https://www.fool.com/earnings/call-transcripts/2026/x/apple-aapl-q4-2025/")
    assert fetcher.fetch_transcript_text(tr).full_text == ""
    assert session.urls == []
    assert list(tmp_path.iterdir()) == []


def test_on_the_fetcher_scrapes(on, tmp_path):
    session = _Session()
    _fetcher(tmp_path, session).search_transcripts("AAPL", limit=1)
    assert len(session.urls) == 1


# --- the API, off ----------------------------------------------------------------

@pytest.fixture()
def live_off(monkeypatch, off):
    import requests

    import finsight.api.app as app_module

    monkeypatch.delenv("FINSIGHT_DEMO", raising=False)
    monkeypatch.setattr(app_module._edgar, "get_cik", lambda t: "0000320193")

    def _no_request(*a, **k):
        raise AssertionError("a request left the process")

    monkeypatch.setattr(requests.Session, "request", _no_request)
    monkeypatch.setattr(app_module._edgar, "get_company_facts", lambda cik: {"facts": {}})
    monkeypatch.setattr(TranscriptFetcher, "_scrape_fool_transcripts", _no_request)
    monkeypatch.setattr(app_module._market, "get_company_profile", lambda t: (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(app_module._market, "get_price_history", lambda t, period: (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(app_module._market, "get_splits", lambda t: [])
    monkeypatch.setattr(app_module._market, "get_peers", lambda t: [])
    return app_module


def test_off_sentiment_answers_not_available_without_a_placeholder(live_off):
    body = TestClient(live_off.app).get("/sentiment/AAPL").json()
    assert body["available"] is False and body["mode"] == "live"
    assert body["results"] == [] and body["synthetic"] is False
    assert body["detail"].startswith("Not available: live earnings-call transcripts are off")
    assert "FINSIGHT_LIVE_TRANSCRIPTS" in body["detail"]


def test_off_the_report_says_not_available_and_scrapes_nothing(live_off):
    text = TestClient(live_off.app).get("/report/AAPL").text
    section = text[text.index("## Earnings Sentiment Analysis"):]
    assert section.startswith("## Earnings Sentiment Analysis\n\nNot available: live earnings-call")
    assert "Synthetic transcript" not in text
    assert "synthetic placeholder" not in text


def test_off_an_unknown_ticker_is_still_a_404(live_off, monkeypatch):
    def _unknown(t):
        raise ValueError("CIK not found")

    monkeypatch.setattr(live_off._edgar, "get_cik", _unknown)
    assert TestClient(live_off.app).get("/sentiment/ZZZZ").status_code == 404


def test_on_sentiment_reports_available(live_off, monkeypatch, on):
    monkeypatch.setattr(live_off, "_transcript_sentiments", lambda t, n: [])
    # No transcript at all for a demo ticker falls back to the labelled placeholder.
    body = TestClient(live_off.app).get("/sentiment/AAPL").json()
    assert body["available"] is True and body["synthetic"] is True


def test_demo_mode_does_not_need_the_flag(monkeypatch, off):
    from finsight.api.app import app

    monkeypatch.setenv("FINSIGHT_DEMO", "1")
    body = TestClient(app).get("/sentiment/AAPL").json()
    assert body["mode"] == "demo" and body["available"] is True and body["synthetic"] is True


# --- a miss is remembered --------------------------------------------------------

def test_a_404_listing_is_asked_once_not_three_times(on, tmp_path):
    session = _Session(status=404)
    results = [_fetcher(tmp_path, session).search_transcripts("AAPL", limit=2) for _ in range(3)]
    assert len(session.urls) == 1
    for found in results:
        assert len(found) == 2 and all(t.synthetic for t in found)


def test_a_listing_with_no_link_is_a_remembered_empty_answer(on, tmp_path):
    session = _Session(status=200, text="<html><body>nothing here</body></html>")
    assert _fetcher(tmp_path, session).search_transcripts("AAPL", limit=2) == []
    assert _fetcher(tmp_path, session).search_transcripts("AAPL", limit=2) == []
    assert len(session.urls) == 1


def test_a_miss_is_per_ticker(on, tmp_path):
    session = _Session(status=404)
    _fetcher(tmp_path, session).search_transcripts("AAPL", limit=1)
    _fetcher(tmp_path, session).search_transcripts("MSFT", limit=1)
    assert len(session.urls) == 2


def test_a_miss_expires(on, tmp_path, monkeypatch):
    session = _Session(status=404)
    now = [1_000_000.0]
    monkeypatch.setattr(transcripts.time, "time", lambda: now[0])
    fetcher = _fetcher(tmp_path, session)
    fetcher.search_transcripts("AAPL", limit=1)
    now[0] += TranscriptFetcher.MISS_TTL_S
    fetcher.search_transcripts("AAPL", limit=1)
    assert len(session.urls) == 1
    now[0] += 1
    fetcher.search_transcripts("AAPL", limit=1)
    assert len(session.urls) == 2


def test_a_miss_is_not_written_to_disk(on, tmp_path):
    _fetcher(tmp_path, _Session(status=404)).search_transcripts("AAPL", limit=1)
    assert list(tmp_path.iterdir()) == []


def test_without_a_cache_every_search_asks(on, tmp_path):
    session = _Session(status=404)
    fetcher = _fetcher(tmp_path, session, cache=False)
    fetcher.search_transcripts("AAPL", limit=1)
    fetcher.search_transcripts("AAPL", limit=1)
    assert len(session.urls) == 2


# --- what the docs say -----------------------------------------------------------

def test_no_doc_calls_the_user_agent_descriptive():
    from pathlib import Path

    root = Path(__file__).parents[1]
    for rel in ("README.md", "SECURITY.md", "docs/architecture.md", "finsight/ingestion/transcripts.py"):
        assert "descriptive User-Agent" not in (root / rel).read_text(encoding="utf-8"), rel
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    assert "names the tool and carries no contact address" in readme


def test_the_readme_states_the_switch_and_the_terms_question():
    from pathlib import Path

    root = Path(__file__).parents[1]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    assert "`FINSIGHT_LIVE_TRANSCRIPTS` | unset" in readme
    assert "has not reviewed whether Motley Fool's terms allow this scrape" in readme
    assert "If you enable live transcripts in production" not in readme
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "# FINSIGHT_LIVE_TRANSCRIPTS=1" in example

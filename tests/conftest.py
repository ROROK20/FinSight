"""Suite-wide guard: no test may reach Yahoo Finance.

yfinance does its own HTTP (not through ``requests``), so the per-test
``requests`` blocks do not cover it. Every Yahoo call goes through
``MarketDataClient._get_ticker``; it raises here, so an un-stubbed call fails
loudly (or is handled as "Yahoo unavailable" by code that guards it) instead
of quietly going out. Tests stub the higher-level methods they need.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_yahoo(monkeypatch):
    from finsight.ingestion.market_data import MarketDataClient

    def _blocked(self, symbol):
        raise RuntimeError(f"Yahoo Finance is not reachable in tests ({symbol})")

    monkeypatch.setattr(MarketDataClient, "_get_ticker", _blocked)


@pytest.fixture(autouse=True)
def _no_remembered_transcript_misses():
    """A miss is remembered per process (transcripts._MISSES): not between tests."""
    from finsight.ingestion import transcripts

    transcripts._MISSES.clear()
    yield
    transcripts._MISSES.clear()

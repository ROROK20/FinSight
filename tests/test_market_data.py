"""The yf.Ticker cache: yfinance holds .info and .splits on the instance for its life."""

import pytest

import finsight.ingestion.market_data as md

# tests/conftest.py blocks this method for every test; here yf.Ticker is the stub instead.
_REAL_GET_TICKER = md.MarketDataClient._get_ticker


@pytest.fixture()
def clock(monkeypatch):
    """A controllable clock and a stubbed yf.Ticker, so nothing reaches Yahoo."""
    now = {"t": 1_000.0}
    monkeypatch.setattr(md.MarketDataClient, "_get_ticker", _REAL_GET_TICKER)
    monkeypatch.setattr("time.monotonic", lambda: now["t"])
    monkeypatch.setattr(md.yf, "Ticker", lambda symbol: object())
    return now


def test_one_ticker_is_reused_within_the_ttl(clock):
    client = md.MarketDataClient()
    first = client._get_ticker("aapl")
    clock["t"] += 60
    assert client._get_ticker("AAPL") is first


def test_a_ticker_past_the_ttl_is_fetched_again(clock):
    """A running API kept the first .info and .splits it saw until it restarted."""
    client = md.MarketDataClient()
    first = client._get_ticker("AAPL")
    clock["t"] += md.TICKER_CACHE_TTL_SECONDS + 1
    assert client._get_ticker("AAPL") is not first


def test_expired_tickers_leave_the_cache(clock):
    client = md.MarketDataClient()
    for symbol in ("AAA", "BBB", "CCC"):
        client._get_ticker(symbol)
    clock["t"] += md.TICKER_CACHE_TTL_SECONDS + 1
    client._get_ticker("DDD")
    assert list(client._ticker_cache) == ["DDD"]


def test_the_ttl_can_be_set_and_the_cache_turned_off(clock):
    client = md.MarketDataClient(cache_ttl=5)
    first = client._get_ticker("AAPL")
    clock["t"] += 6
    assert client._get_ticker("AAPL") is not first
    off = md.MarketDataClient(cache=False)
    assert off._get_ticker("AAPL") is not off._get_ticker("AAPL")
    assert off._ticker_cache == {}

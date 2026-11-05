"""
Market data client for price, fundamentals, and peer data.

Uses yfinance as the primary source (free, no API key required).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# How long one yf.Ticker is reused. yfinance holds .info and .splits on the instance for
# its life, so a long-running API kept answering with the first ones it saw: a split
# made after that was never seen, and the price-basis check depends on it.
TICKER_CACHE_TTL_SECONDS = 900.0


# Hand-picked peer sets for major tickers. Every symbol must be LISTED: a delisted
# one can only fail its profile fetch and be left out of the comps, named as unavailable.
CURATED_PEERS: dict[str, tuple[str, ...]] = {
    "AAPL": ("MSFT", "GOOGL", "META", "AMZN", "NVDA"),
    "MSFT": ("AAPL", "GOOGL", "META", "AMZN", "ORCL"),
    "GOOGL": ("MSFT", "META", "AMZN", "SNAP", "TTD"),
    "META": ("SNAP", "PINS", "GOOGL", "RDDT", "TTD"),
    "AMZN": ("MSFT", "GOOGL", "AAPL", "WMT", "TGT"),
    "NVDA": ("AMD", "INTC", "QCOM", "AVGO", "TSM"),
    "JPM": ("BAC", "WFC", "GS", "MS", "C"),
    "TSLA": ("RIVN", "NIO", "GM", "F", "LI"),
    "JNJ": ("PFE", "ABBV", "MRK", "BMY", "AMGN"),
}


def no_curated_peers(ticker: str) -> str:
    """Why a ticker's report has no comps: FinSight's peers are this list, and
    Yahoo Finance supplies only their multiples. It looks no peers up."""
    return (f"{ticker.upper()} has no curated peer set. FinSight's peers are a hand-picked "
            f"list for {', '.join(sorted(CURATED_PEERS))} only; it does not look peers up.")


@dataclass
class CompanyProfile:
    """Basic company profile from Yahoo Finance."""

    ticker: str
    name: str
    sector: str
    industry: str
    market_cap: float
    enterprise_value: float
    pe_ratio: float | None
    pb_ratio: float | None
    ps_ratio: float | None
    ev_ebitda: float | None
    debt_to_equity: float | None
    roe: float | None
    roa: float | None
    gross_margin: float | None
    operating_margin: float | None
    net_margin: float | None
    revenue_ttm: float | None
    ebitda_ttm: float | None
    free_cash_flow: float | None
    beta: float | None
    dividend_yield: float | None
    description: str = ""
    currency: str | None = None
    """The currency the price, market cap and enterprise value are in."""
    financial_currency: str | None = None
    """The currency revenue, EBITDA and free cash flow are reported in. An ADR
    can differ from ``currency``: TSM, NIO and LI trade in USD and report in TWD
    or CNY."""

    def __repr__(self) -> str:
        return (
            f"CompanyProfile({self.ticker} | {self.name} | "
            f"MCap=${self.market_cap/1e9:.1f}B | P/E={self.pe_ratio})"
        )


class MarketDataClient:
    """
    Fetches market data, fundamentals, and peer information.

    Parameters
    ----------
    cache : bool
        Cache ticker objects to avoid redundant API calls.
    cache_ttl : float
        Seconds a cached ticker object is reused; an older one is dropped and refetched.

    Examples
    --------
    >>> client = MarketDataClient()
    >>> profile = client.get_company_profile("AAPL")
    >>> prices = client.get_price_history("AAPL", period="2y")
    >>> peers = client.get_peers("AAPL")
    """

    def __init__(self, cache: bool = True, cache_ttl: float = TICKER_CACHE_TTL_SECONDS) -> None:
        self._ticker_cache: dict[str, tuple[float, yf.Ticker]] = {}
        self.cache = cache
        self.cache_ttl = cache_ttl

    def _get_ticker(self, symbol: str) -> yf.Ticker:
        symbol = symbol.upper()
        if not self.cache:
            return yf.Ticker(symbol)
        now = time.monotonic()
        held = self._ticker_cache.get(symbol)
        if held is not None and now - held[0] < self.cache_ttl:
            return held[1]
        # Expired entries go too, so the cache holds only what was asked for lately.
        self._ticker_cache = {s: entry for s, entry in self._ticker_cache.items()
                              if now - entry[0] < self.cache_ttl}
        t = yf.Ticker(symbol)
        self._ticker_cache[symbol] = (now, t)
        return t

    def get_company_profile(self, ticker: str) -> CompanyProfile:
        """
        Fetch comprehensive company profile and valuation multiples.

        Parameters
        ----------
        ticker : str
            Stock ticker symbol.

        Returns
        -------
        CompanyProfile
        """
        t = self._get_ticker(ticker)
        info = t.info or {}

        def safe_get(key: str, default=None):
            return info.get(key, default)

        return CompanyProfile(
            ticker=ticker.upper(),
            name=safe_get("longName", ticker),
            sector=safe_get("sector", ""),
            industry=safe_get("industry", ""),
            market_cap=safe_get("marketCap", 0) or 0,
            enterprise_value=safe_get("enterpriseValue", 0) or 0,
            pe_ratio=safe_get("trailingPE"),
            pb_ratio=safe_get("priceToBook"),
            ps_ratio=safe_get("priceToSalesTrailing12Months"),
            ev_ebitda=safe_get("enterpriseToEbitda"),
            debt_to_equity=safe_get("debtToEquity"),
            roe=safe_get("returnOnEquity"),
            roa=safe_get("returnOnAssets"),
            gross_margin=safe_get("grossMargins"),
            operating_margin=safe_get("operatingMargins"),
            net_margin=safe_get("profitMargins"),
            revenue_ttm=safe_get("totalRevenue"),
            ebitda_ttm=safe_get("ebitda"),
            free_cash_flow=safe_get("freeCashflow"),
            beta=safe_get("beta"),
            dividend_yield=safe_get("dividendYield"),
            description=safe_get("longBusinessSummary", ""),
            currency=safe_get("currency"),
            financial_currency=safe_get("financialCurrency"),
        )

    def get_splits(self, ticker: str) -> list[dict]:
        """Stock splits as Yahoo Finance reports them: ``[{"date", "ratio"}]``.

        Yahoo's prices are split-adjusted while a 10-K's share count is as
        reported, so a split between the two puts them on different bases.
        """
        series = self._get_ticker(ticker).splits
        if series is None or len(series) == 0:
            return []
        return [{"date": idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10],
                 "ratio": float(v)} for idx, v in series.items() if v and float(v) > 0]

    def get_price_history(
        self,
        ticker: str,
        period: str = "2y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Fetch OHLCV price history.

        Parameters
        ----------
        ticker : str
            Ticker symbol.
        period : str
            History period: '1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'.
        interval : str
            Bar interval: '1d', '1wk', '1mo'.

        Returns
        -------
        pd.DataFrame with OHLCV columns.
        """
        t = self._get_ticker(ticker)
        df = t.history(period=period, interval=interval)
        if df.empty:
            logger.warning(f"No price data for {ticker}")
        return df

    def get_financials(self, ticker: str) -> dict[str, pd.DataFrame]:
        """
        Fetch structured financial statements.

        Returns
        -------
        dict with keys: 'income', 'balance_sheet', 'cash_flow'.
        """
        t = self._get_ticker(ticker)
        return {
            "income": t.financials.T if t.financials is not None else pd.DataFrame(),
            "balance_sheet": t.balance_sheet.T if t.balance_sheet is not None else pd.DataFrame(),
            "cash_flow": t.cashflow.T if t.cashflow is not None else pd.DataFrame(),
        }

    def get_quarterly_financials(self, ticker: str) -> dict[str, pd.DataFrame]:
        """Fetch quarterly financial statements."""
        t = self._get_ticker(ticker)
        return {
            "income": t.quarterly_financials.T if t.quarterly_financials is not None else pd.DataFrame(),
            "balance_sheet": t.quarterly_balance_sheet.T if t.quarterly_balance_sheet is not None else pd.DataFrame(),
            "cash_flow": t.quarterly_cashflow.T if t.quarterly_cashflow is not None else pd.DataFrame(),
        }

    def get_peers(self, ticker: str) -> list[str]:
        """
        Get the curated peer set for a major ticker.

        Only the tickers in :data:`CURATED_PEERS` have peers; any other ticker
        gets an empty list (there is no sector or ETF fallback, and no request
        is made).

        Parameters
        ----------
        ticker : str
            Ticker symbol.

        Returns
        -------
        list[str]
            List of peer ticker symbols.
        """
        ticker_upper = ticker.upper()
        if ticker_upper in CURATED_PEERS:
            return list(CURATED_PEERS[ticker_upper])

        logger.info(f"No curated peers for {ticker}; comps will be skipped.")
        return []

    def get_consensus_estimates(self, ticker: str) -> dict:
        """
        Fetch analyst consensus estimates.

        Returns
        -------
        dict with eps_estimates, revenue_estimates, recommendations.
        """
        t = self._get_ticker(ticker)
        result: dict = {}
        try:
            result["eps_estimates"] = t.eps_trend.to_dict() if t.eps_trend is not None else {}
            result["revenue_estimates"] = t.revenue_estimate.to_dict() if t.revenue_estimate is not None else {}
            result["recommendations"] = t.recommendations.tail(20).to_dict() if t.recommendations is not None else {}
        except Exception as e:
            logger.warning(f"Could not fetch consensus for {ticker}: {e}")
        return result

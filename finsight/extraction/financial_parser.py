"""
Financial statement parser for Yahoo Finance / EDGAR structured data.

Converts raw financial statement DataFrames into clean, normalized formats
suitable for DCF modeling and comps analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class FinancialHistory:
    """Multi-year financial history for a company."""

    ticker: str
    income_statement: pd.DataFrame = field(default_factory=pd.DataFrame)
    balance_sheet: pd.DataFrame = field(default_factory=pd.DataFrame)
    cash_flow: pd.DataFrame = field(default_factory=pd.DataFrame)
    quarterly_income: pd.DataFrame = field(default_factory=pd.DataFrame)
    quarterly_cash_flow: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def revenue_history(self) -> pd.Series:
        return self._get_series(self.income_statement, [
            "Total Revenue", "Revenue", "Revenues", "Net Sales"
        ])

    @property
    def ebitda_history(self) -> pd.Series:
        ebit = self._get_series(self.income_statement, ["EBIT", "Operating Income"])
        da = self._get_series(self.cash_flow, ["Depreciation Amortization Depletion",
                                                "Depreciation And Amortization"])
        if not ebit.empty and not da.empty:
            return (ebit + da).dropna()
        return ebit

    @property
    def fcf_history(self) -> pd.Series:
        return self._get_series(self.cash_flow, [
            "Free Cash Flow", "FreeCashFlow",
        ])

    def _get_series(self, df: pd.DataFrame, candidates: list[str]) -> pd.Series:
        if df.empty:
            return pd.Series(dtype=float)
        for col in candidates:
            if col in df.columns:
                return df[col].dropna().astype(float)
        return pd.Series(dtype=float)

    def __repr__(self) -> str:
        n_years = len(self.income_statement)
        return f"FinancialHistory({self.ticker} | {n_years} years)"


class FinancialParser:
    """
    Parses and normalizes financial data from various sources.

    Methods
    -------
    parse_yfinance(ticker, statements) → FinancialHistory
    compute_growth_rates(series) → dict
    compute_margins(income_df) → dict
    ttm(quarterly_df, metric) → float
    """

    def parse_yfinance(
        self,
        ticker: str,
        statements: dict[str, pd.DataFrame],
    ) -> FinancialHistory:
        """
        Parse yfinance financial statements into FinancialHistory.

        Parameters
        ----------
        ticker : str
            Ticker symbol for metadata.
        statements : dict
            Keys: 'income', 'balance_sheet', 'cash_flow'.

        Returns
        -------
        FinancialHistory
        """
        return FinancialHistory(
            ticker=ticker,
            income_statement=statements.get("income", pd.DataFrame()),
            balance_sheet=statements.get("balance_sheet", pd.DataFrame()),
            cash_flow=statements.get("cash_flow", pd.DataFrame()),
        )

    def compute_growth_rates(self, series: pd.Series) -> dict[str, float | None]:
        """
        Compute YoY and CAGR growth rates from a time series.

        Parameters
        ----------
        series : pd.Series
            Annual values, most recent last.

        Returns
        -------
        dict with: yoy_1y, yoy_2y, yoy_3y, cagr_3y, cagr_5y.
        """
        result: dict[str, float | None] = {}

        s = series.dropna().sort_index()
        if len(s) < 2:
            return result

        vals = s.values.astype(float)

        def yoy(n: int) -> float | None:
            if len(vals) > n and vals[-n - 1] != 0:
                return (vals[-1] / vals[-n - 1]) - 1
            return None

        def cagr(n: int) -> float | None:
            if len(vals) > n and vals[-n - 1] > 0:
                return (vals[-1] / vals[-n - 1]) ** (1 / n) - 1
            return None

        result["yoy_1y"] = yoy(1)
        result["yoy_2y"] = yoy(2)
        result["yoy_3y"] = yoy(3)
        result["cagr_3y"] = cagr(3)
        result["cagr_5y"] = cagr(5)

        return result

    def compute_margins(self, income_df: pd.DataFrame) -> dict[str, pd.Series]:
        """
        Compute margin history from income statement.

        Returns
        -------
        dict with: gross_margin, operating_margin, net_margin (each as pd.Series).
        """
        result = {}

        def div_series(num_col: str, den_col: str) -> pd.Series:
            candidates_num = [c for c in income_df.columns if num_col.lower() in c.lower()]
            candidates_den = [c for c in income_df.columns if den_col.lower() in c.lower()]
            if not candidates_num or not candidates_den:
                return pd.Series(dtype=float)
            num = income_df[candidates_num[0]].astype(float)
            den = income_df[candidates_den[0]].astype(float)
            return (num / den).replace([np.inf, -np.inf], np.nan)

        result["gross_margin"] = div_series("Gross Profit", "Total Revenue")
        result["operating_margin"] = div_series("Operating Income", "Total Revenue")
        result["net_margin"] = div_series("Net Income", "Total Revenue")

        return result

    def ttm(self, quarterly_df: pd.DataFrame, metric: str) -> float | None:
        """
        Compute trailing twelve months (TTM) value for a metric.

        Parameters
        ----------
        quarterly_df : pd.DataFrame
            Quarterly income/cash flow statement.
        metric : str
            Column name to aggregate.

        Returns
        -------
        float | None
            Sum of last 4 quarters.
        """
        if quarterly_df.empty:
            return None
        candidates = [c for c in quarterly_df.columns if metric.lower() in c.lower()]
        if not candidates:
            return None
        series = quarterly_df[candidates[0]].dropna().astype(float)
        if len(series) < 4:
            return float(series.sum())
        return float(series.iloc[-4:].sum())

    def normalize_xbrl_to_df(
        self,
        xbrl_series: dict[str, list[dict]],
        concepts: list[str],
        period_type: str = "annual",
    ) -> pd.DataFrame:
        """
        Convert XBRL concept series to a tidy DataFrame.

        Parameters
        ----------
        xbrl_series : dict
            Map of concept → list of {end, val, form} entries.
        concepts : list[str]
            Concepts to include as columns.
        period_type : str
            'annual' (FY) or 'quarterly'.

        Returns
        -------
        pd.DataFrame indexed by period end date, columns = concepts.
        """
        form_filter = "10-K" if period_type == "annual" else "10-Q"
        data: dict[str, dict] = {}

        for concept in concepts:
            entries = xbrl_series.get(concept, [])
            for e in entries:
                if e.get("form") != form_filter:
                    continue
                end = e.get("end", "")
                if end not in data:
                    data[end] = {}
                data[end][concept] = float(e["val"]) / 1e6  # → $M

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame.from_dict(data, orient="index")
        df.index = pd.to_datetime(df.index, errors="coerce")
        df = df.dropna(how="all").sort_index()
        return df

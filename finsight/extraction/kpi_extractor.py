"""
KPI extraction from SEC filings and earnings transcripts.

Two paths:

* **XBRL** (``extract_from_companyfacts`` / ``extract_from_xbrl``) — the
  structured figures filers tag in their 10-Ks, read for one fiscal year with
  per-figure provenance. Deterministic and keyless; this is the path the REST
  API (``/kpis``, ``/dcf``, ``/report``) and demo mode use.
* **Text** (``extract_from_text``) — a library function, not wired to any API
  endpoint: an OpenAI-compatible LLM pulls KPIs out of unstructured filing or
  transcript text when ``OPENAI_API_KEY`` is set, with a regex fallback
  otherwise.

Environment variables (text path only):
  OPENAI_API_KEY   — OpenAI API key
  OPENAI_BASE_URL  — Optional: custom base URL (e.g. for local Ollama)
  OPENAI_MODEL     — Model to use (default: gpt-4o-mini)
"""

from __future__ import annotations

import os
import re
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from finsight.extraction.financial_parser import FinancialParser
from finsight.ingestion.edgar import (
    ANNUAL_MAX_DAYS,
    ANNUAL_MIN_DAYS,
    extract_financial_series,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"

_EXTRACTION_SYSTEM_PROMPT = """You are a financial analyst expert at extracting structured data
from SEC filings and earnings call transcripts. Extract the requested financial metrics precisely.
Always respond with valid JSON. If a value cannot be found, use null.
Express monetary values in millions of USD. Express percentages as decimals (e.g. 0.15 for 15%).
"""

_KPI_EXTRACTION_PROMPT = """Extract the following financial KPIs from this text:
- revenue (total revenue, in $M)
- revenue_growth_yoy (year-over-year %, decimal)
- gross_profit (in $M)
- gross_margin (decimal)
- operating_income (EBIT, in $M)
- operating_margin (decimal)
- net_income (in $M)
- net_margin (decimal)
- ebitda (in $M)
- eps_diluted (earnings per diluted share, $)
- eps_growth_yoy (decimal)
- free_cash_flow (in $M)
- total_debt (in $M)
- cash_and_equivalents (in $M)
- capex (in $M)
- r_and_d_expense (in $M)
- shares_outstanding (diluted, in millions)
- guidance_revenue_low (low end of FULL-YEAR revenue guidance, $M; null if only quarterly guidance is given)
- guidance_revenue_high (high end of FULL-YEAR revenue guidance, $M; null if only quarterly guidance is given)
- guidance_period ("annual" if the two guidance figures are for a full fiscal year, "quarterly" if the only guidance given is for a quarter, null if none)
- key_risks (list of top 3 risk factors, strings)
- growth_drivers (list of top 3 growth drivers, strings)

Text to analyze:
{text}

Respond ONLY with a JSON object matching these exact keys. Use null for missing values."""


# Money units the regex fallback understands, longest first, and their $M factor.
_UNIT_TO_MILLIONS = {
    "trillion": 1e6, "tn": 1e6, "t": 1e6,
    "billion": 1e3, "bn": 1e3, "b": 1e3,
    "million": 1.0, "mn": 1.0, "mm": 1.0, "m": 1.0,
    "thousand": 1e-3, "k": 1e-3,
    "": 1.0,
}
_UNIT_WORDS = "|".join(sorted((u for u in _UNIT_TO_MILLIONS if u), key=len, reverse=True))
REGEX_FIELDS = ("revenue", "net_income", "ebitda", "free_cash_flow", "gross_margin",
                "operating_margin")
"""The fields the no-key regex fallback reads; everything else stays None."""

# How the regex fallback reads a sentence. It wants the field's LEVEL, with its sign,
# or nothing: "Free cash flow was negative $1.2 billion" is -1,200, "Net income was
# -$3.0 billion" -3,000, and "Revenue increased $5.1 billion to $94.9 billion" 94,900.
_MINUS_SIGNS = "-−–"
# A minus must touch the figure: "Revenue - $94.9 billion" is a separator, not a sign.
_MONEY_FIGURE = re.compile(
    rf"(?P<open>\(\s*)?(?P<lead>(?<![\w$])[{_MINUS_SIGNS}])?\$\s*(?P<inner>[{_MINUS_SIGNS}]|\(\s*)?"
    rf"(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<shut>\)\s*)?(?P<unit>{_UNIT_WORDS})?(?![a-z])"
    r"\s*(?P<close>\))?",
    re.IGNORECASE,
)
_PERCENT_FIGURE = re.compile(
    rf"(?P<open>\(\s*)?(?P<lead>(?<![\w.$])[{_MINUS_SIGNS}])?(?P<number>\d+(?:\.\d*)?)\s*"
    r"(?P<shut>\)\s*)?%\s*(?P<close>\))?"
)
_SENTENCE_END = re.compile(r"(?<=[.!?;])(?<!\.[a-zA-Z]\.)\s+")
_PARENTHETICAL = re.compile(r"\([^()]*\)")
_PER_UNIT = re.compile(r"s?\s+per\b", re.IGNORECASE)
# Another field named before the figure: the figure is that field's.
_OTHER_FIELD = re.compile(
    r"\b(?:revenues?|net\s+sales|net\s+income|net\s+loss(?:es)?|ebitda|free\s+cash\s+flow|"
    r"gross\s+margin|operating\s+margin|operating\s+income|operating\s+loss|gross\s+profit|"
    r"cost\s+of)\b",
    re.IGNORECASE,
)
_CHANGE_WORD = re.compile(
    r"\b(?:increas\w*|decreas\w*|rose|ris(?:e|es|en|ing)|fell|fall(?:s|en|ing)?|grew|"
    r"grow(?:s|n|th|ing)?|declin\w*|drop\w*|climb\w*|jump\w*|gain\w*|slid\w*|slip\w*|"
    r"shr[aiu]nk\w*|improv\w*|widen\w*|narrow\w*|expand\w*|expansion|contracted|contraction|"
    r"compress\w*|reduc\w*|soar\w*|surg\w*|up|down|higher|lower)\b",
    re.IGNORECASE,
)
_LEVEL_WORDS = (r"(?<!due )(?<!owing )(?<!thanks )(?<!compared )(?<!relative )(?<!according )\bto\b"
                r"|\breach(?:ed|es|ing)\b|\btotal(?:l?ed|l?ing|s)\b")
# What says the figure after a change word is the level: "increased 8% to $94.9 billion".
_LEVEL_AFTER_CHANGE = re.compile(_LEVEL_WORDS, re.IGNORECASE)
_LEVEL_IN_HEAD = re.compile(
    rf"{_LEVEL_WORDS}|,\s*(?:and\s+)?(?:was|were|is|are)\b|\bat\s*$", re.IGNORECASE)
# The figure is the earlier year's: "rose from $80 billion to…", "compared with $80 billion".
_PRIOR_FIGURE = re.compile(r"\b(?:from|compared\s+(?:with|to)|versus|vs\.?)\s*$", re.IGNORECASE)
# The figure is a difference: "$5.1 billion higher", "a $5.1 billion increase".
_DIFFERENCE_AFTER = re.compile(
    r"\s*(?:,\s*or\s+[\d.]+\s*%\s*,\s*)?(?:in\s+)?(?:(?:higher|lower|above|below|ahead|better|"
    r"worse|greater|increase|decrease|decline|drop|gain|rise|jump|improvement|reduction|growth)\b"
    r"|(?:more|less)\s+than\s+(?:last|the\s+(?:prior|previous|same)|a\s+year|in\b))",
    re.IGNORECASE,
)
_NEGATIVE_END = re.compile(r"\bnegative\s*(?:of|at)?\s*$", re.IGNORECASE)
_LOSS_WORDS = r"loss(?:es)?|outflow|deficit|burn|shortfall"
_LOSS_END = re.compile(
    rf"\b(?:net\s+)?(?:{_LOSS_WORDS})\s+(?:of|was|were|is|at|totall?ing|totall?ed)\s*$",
    re.IGNORECASE)
_LOSS_AFTER = re.compile(rf"\s*(?:net\s+)?(?:{_LOSS_WORDS}|negative)\b", re.IGNORECASE)
# A loss word must be what the field WAS. "Net income reflects a loss of $1 billion" says
# what the field was hit by, and what the field was is not in the sentence.
_STATES_THE_FIELD = re.compile(
    r"^[\s:=–—-]*(?:\(loss\)\s*)?(?:[^,;]*?\b(?:was|were|is|are|came\s+in(?:\s+at)?|turned|"
    r"remained|of|at)\s+)?(?:(?:a|an)\s+)?(?:net\s+|total\s+|cash\s+)?$",
    re.IGNORECASE,
)
# Accounting brackets, or a prose aside? "Free cash flow was ($1.2 billion)" is the first,
# "Net income for the year ($3 billion) rose 5%" the second.
_BRACKET_IS_A_SIGN = re.compile(
    r"(?:\b(?:was|were|is|are|of|at|to|came\s+in\s+at|totall?ed|totall?ing|reached|reaching)"
    r"|[:=–—-])\s*$",
    re.IGNORECASE,
)
_OPENS_A_LINE = re.compile(
    r"(?:^|\n|[.!?;:]\s)\s*(?:(?:adjusted|total|consolidated|reported|gaap|non-gaap)\s+){0,2}$",
    re.IGNORECASE,
)
_CLOSES_A_LINE = re.compile(r"[ \t]*(?:[.;]?[ \t]*(?:\n|$)|[.;]\s)")


def _sign_of(
    figure: re.Match, head: str, after: str, can_lose: bool,
    opens_line: bool = False, introduced: bool = False,
) -> int | None:
    """-1 or +1 for a figure read as a level, or None when the sentence does not settle it.

    ``head`` and ``after`` are the words either side of the figure, ``introduced`` that a
    word such as "to" said it is the level, and ``can_lose`` False for a field whose "loss
    of $X" is never its level (revenue).
    """
    if figure.group("lead") or figure.groupdict().get("inner"):
        return -1
    if figure.group("open") and (figure.group("shut") or figure.group("close")):
        if introduced or _BRACKET_IS_A_SIGN.search(head):
            return -1
        if not can_lose:
            return 1              # "Revenue ($94.9 billion) rose 5%"
        table_row = not head.strip() and opens_line and _CLOSES_A_LINE.match(after)
        return -1 if table_row else None
    negative = _NEGATIVE_END.search(head)
    loss = _LOSS_END.search(head)
    if not (negative or loss or _LOSS_AFTER.match(after)):
        return 1
    if not (can_lose or negative):
        return None
    stated = negative or loss
    return -1 if _STATES_THE_FIELD.match(head[:stated.start()] if stated else head) else None


def _level_after_change(after: str, figure: re.Pattern, value, can_lose: bool) -> float | None:
    """The level a sentence gives after stating a change, or None if it gives only the change."""
    sentence = _SENTENCE_END.split(after, maxsplit=1)[0]
    for word in _LEVEL_AFTER_CHANGE.finditer(sentence):
        tail = sentence[word.end():]
        found = figure.search(tail)
        if found is None:
            return None
        head = tail[:found.start()]
        if re.search(r"\d", head) or _OTHER_FIELD.search(head):
            continue
        sign = _sign_of(found, head, tail[found.end():], can_lose, introduced=True)
        return None if sign is None else sign * value(found)
    return None


def _read_level(
    text: str, label: str, figure: re.Pattern, value, can_lose: bool = True,
) -> float | None:
    """The first mention of ``label`` whose sentence states the field's level, signed.

    A figure counts only in the label's own sentence, with no other field and (unless a
    change word explains them) no other number before it. A sentence that states only a
    change gives no level: the next mention is tried, and otherwise the field stays empty.
    A "net loss" label makes the level negative whichever way it is written.
    """
    for mention in re.finditer(label, text, re.IGNORECASE):
        # Searched in place, so "Revenue-$94.9 billion" shows its minus touching a word.
        found = None if _PER_UNIT.match(text, mention.end()) else figure.search(text, mention.end())
        if found is None:
            continue
        head, after = text[mention.end():found.start()], text[found.end():]
        if _SENTENCE_END.search(head) or _OTHER_FIELD.search(_PARENTHETICAL.sub("", head)):
            continue
        changes = list(_CHANGE_WORD.finditer(head))
        said_level = _LEVEL_IN_HEAD.search(head, changes[-1].end()) if changes else None
        if (changes and not said_level) or _PRIOR_FIGURE.search(head) or _DIFFERENCE_AFTER.match(after):
            level = _level_after_change(after, figure, value, can_lose)
        elif not changes and re.search(r"\d", head):
            continue
        else:
            sign = _sign_of(
                found, head[said_level.end():] if said_level else head, after, can_lose,
                opens_line=bool(_OPENS_A_LINE.search(text[:mention.start()])),
                introduced=bool(said_level),
            )
            level = None if sign is None else sign * value(found)
        if level is not None:
            return -abs(level) if "loss" in mention.group(0).lower() else level
    return None


@dataclass
class ExtractedKPIs:
    """Structured KPIs extracted from a filing or transcript."""

    ticker: str
    period: str
    source_type: str  # '10-K', '10-Q', '8-K', 'transcript'

    revenue: float | None = None
    revenue_growth_yoy: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None
    operating_income: float | None = None
    operating_margin: float | None = None
    net_income: float | None = None
    net_margin: float | None = None
    ebitda: float | None = None
    eps_diluted: float | None = None
    eps_growth_yoy: float | None = None
    free_cash_flow: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    capex: float | None = None
    r_and_d_expense: float | None = None
    depreciation: float | None = None
    """Depreciation of property and equipment (us-gaap:Depreciation), $M."""
    amortization_of_intangibles: float | None = None
    """Amortization of acquired intangibles (us-gaap:AmortizationOfIntangibleAssets), $M."""
    shares_outstanding: float | None = None
    guidance_revenue_low: float | None = None
    guidance_revenue_high: float | None = None
    guidance_period: str | None = None
    """"annual" or "quarterly": the DCF uses revenue guidance only when it is
    annual (a quarter's guidance against annual revenue read as −74% growth)."""
    key_risks: list[str] = field(default_factory=list)
    growth_drivers: list[str] = field(default_factory=list)

    extraction_method: str = "llm"  # 'llm', 'regex', or 'xbrl'
    provenance: dict[str, Any] = field(default_factory=dict)
    """Where every figure came from (XBRL path): fiscal year and year-end,
    filing accession, the concept or formula behind each field, and the fields
    that were not reported."""

    @property
    def ev_ebitda_ratio(self) -> float | None:
        """Placeholder — requires enterprise value from market data."""
        return None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def __repr__(self) -> str:
        return (
            f"ExtractedKPIs({self.ticker} | {self.period} | "
            f"rev=${self.revenue}M | margin={self.net_margin})"
        )


class KPIExtractor:
    """
    Extracts structured financial KPIs from SEC XBRL data or filing text.

    XBRL (:meth:`extract_from_companyfacts`) is deterministic and keyless. For
    unstructured text (:meth:`extract_from_text`): if OPENAI_API_KEY is set,
    an LLM extracts structured JSON; otherwise regex patterns are used.

    Parameters
    ----------
    model : str
        OpenAI model to use (default: gpt-4o-mini, cheapest capable model).
    max_text_chars : int
        Truncate input text to this length before sending to LLM (default 8000).

    Examples
    --------
    >>> extractor = KPIExtractor()
    >>> kpis = extractor.extract_from_text(filing_text, ticker="AAPL",
    ...                                      period="FY2023", source_type="10-K")
    >>> print(kpis.revenue, kpis.gross_margin)
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        max_text_chars: int = 8_000,
    ) -> None:
        self.model = os.getenv("OPENAI_MODEL", model)
        self.max_text_chars = max_text_chars
        self._client = self._init_client()

    def _init_client(self):
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logger.info(
                "OPENAI_API_KEY not set: extract_from_text() will use the regex "
                "fallback. (XBRL extraction, used by the API, needs no key.)"
            )
            return None
        try:
            from openai import OpenAI
            return OpenAI(
                api_key=api_key,
                base_url=os.getenv("OPENAI_BASE_URL"),
            )
        except ImportError:
            logger.warning("openai package not installed. Run: pip install openai")
            return None

    def extract_from_text(
        self,
        text: str,
        ticker: str,
        period: str,
        source_type: str = "10-K",
    ) -> ExtractedKPIs:
        """
        Extract KPIs from a block of text.

        Reads only the first ``max_text_chars`` characters (8,000 by default),
        for the LLM and the regex fallback alike: pass the section that holds
        the figures, since a whole filing's opening is its cover page.

        Parameters
        ----------
        text : str
            Raw text from a filing or transcript.
        ticker : str
            Company ticker for metadata.
        period : str
            Period string (e.g. 'FY2023', 'Q3 2023').
        source_type : str
            Filing type for metadata.

        Returns
        -------
        ExtractedKPIs
        """
        truncated = text[:self.max_text_chars]

        if self._client is not None:
            return self._extract_llm(truncated, ticker, period, source_type)
        return self._extract_regex(truncated, ticker, period, source_type)

    # Which us-gaap concept(s) feed each figure, in priority order, with the
    # unit each is filed under. A concept is only used at the EXACT fiscal
    # year-end being extracted — a filer that stopped using `Revenues` in 2018
    # must never have its 2018 figure reported as this year's.
    XBRL_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
        # `Revenues` first: when a filer reports it, it is the total top line;
        # the ASC 606 concept (2018+) can exclude e.g. leasing revenue.
        "revenue": (
            ("Revenues", "USD"),
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            ("RevenueFromContractWithCustomerIncludingAssessedTax", "USD"),
            ("SalesRevenueNet", "USD"),
        ),
        "cost_of_revenue": (
            ("CostOfRevenue", "USD"),
            ("CostOfGoodsAndServicesSold", "USD"),
        ),
        "gross_profit": (("GrossProfit", "USD"),),
        "operating_income": (("OperatingIncomeLoss", "USD"),),
        "net_income": (("NetIncomeLoss", "USD"),),
        # TOTAL depreciation and amortization, as the cash-flow statement adds
        # it back. `Depreciation` alone (property and equipment only) is a
        # separate, last-resort chain below — see the EBITDA derivation.
        "depreciation_amortization": (
            ("DepreciationDepletionAndAmortization", "USD"),
            ("DepreciationAndAmortization", "USD"),
            ("DepreciationAmortizationAndAccretionNet", "USD"),
        ),
        "depreciation_only": (("Depreciation", "USD"),),
        "amortization_of_intangibles": (("AmortizationOfIntangibleAssets", "USD"),),
        "operating_cash_flow": (("NetCashProvidedByUsedInOperatingActivities", "USD"),),
        "capex": (
            ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
            ("PaymentsToAcquireProductiveAssets", "USD"),
        ),
        "long_term_debt": (("LongTermDebt", "USD"),),
        "long_term_debt_noncurrent": (("LongTermDebtNoncurrent", "USD"),),
        "long_term_debt_current": (("LongTermDebtCurrent", "USD"),),
        # Debt due within a year. Unlike LongTermDebtCurrent, DebtCurrent includes
        # short-term borrowings by definition, so it is never added to them.
        "debt_current": (("DebtCurrent", "USD"),),
        # The balance sheet's two lines, each debt AND finance leases, and the leases
        # inside them: what shows whether LongTermDebt holds the current part.
        "debt_and_leases_current": (("LongTermDebtAndCapitalLeaseObligationsCurrent", "USD"),),
        "debt_and_leases_noncurrent": (("LongTermDebtAndCapitalLeaseObligations", "USD"),),
        "finance_leases_current": (("FinanceLeaseLiabilityCurrent", "USD"),),
        "finance_leases_noncurrent": (("FinanceLeaseLiabilityNoncurrent", "USD"),),
        "short_term_debt": (
            ("CommercialPaper", "USD"),
            ("ShortTermBorrowings", "USD"),
        ),
        # Common long-term tags for a filer with neither LongTermDebt nor
        # LongTermDebtNoncurrent (_wider_long_term_debt). This is a list of common tags,
        # not of every tag: debt under any other one is not read, and the total reads low.
        "debt_and_leases_total": (
            ("LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", "USD"),),
        "notes_total": (("NotesPayable", "USD"),),
        "notes_noncurrent": (("LongTermNotesPayable", "USD"), ("NotesPayableNoncurrent", "USD"),),
        "notes_current": (("NotesPayableCurrent", "USD"),),
        "senior_notes_total": (("SeniorNotes", "USD"), ("SeniorLongTermNotes", "USD"),),
        "senior_notes_noncurrent": (("SeniorNotesNoncurrent", "USD"),),
        "senior_notes_current": (("SeniorNotesCurrent", "USD"),),
        "convertible_notes_total": (("ConvertibleNotesPayable", "USD"),),
        "convertible_notes_noncurrent": (("ConvertibleLongTermNotesPayable", "USD"),
                                         ("ConvertibleNotesPayableNoncurrent", "USD"),),
        "convertible_notes_current": (("ConvertibleNotesPayableCurrent", "USD"),),
        "cash_and_equivalents": (("CashAndCashEquivalentsAtCarryingValue", "USD"),),
        "r_and_d_expense": (("ResearchAndDevelopmentExpense", "USD"),),
        "diluted_shares": (("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),),
        "period_end_shares": (("CommonStockSharesOutstanding", "shares"),),
        "eps_diluted": (("EarningsPerShareDiluted", "USD/shares"),),
    }

    # The KPI fields a complete extraction fills; anything left None is listed
    # in provenance["missing"] so a consumer never mistakes absence for zero.
    _REPORTED_FIELDS = (
        "revenue", "revenue_growth_yoy", "gross_profit", "operating_income",
        "net_income", "ebitda", "eps_diluted", "free_cash_flow", "total_debt",
        "cash_and_equivalents", "capex", "r_and_d_expense", "shares_outstanding",
    )

    def extract_from_companyfacts(
        self,
        facts: dict[str, Any],
        ticker: str,
        period: str | None = None,
        source: str = "SEC EDGAR XBRL companyfacts",
    ) -> ExtractedKPIs:
        """
        Extract one fiscal year's KPIs from a raw SEC companyfacts payload.

        Reads every concept in :attr:`XBRL_CONCEPTS` with its correct unit and
        annual-only filtering, then delegates to :meth:`extract_from_xbrl`.
        This is the single loader behind the API's ``/kpis``, ``/dcf`` and
        ``/report`` and behind demo mode, so all four describe the same year
        from the same concepts.

        Parameters
        ----------
        facts : dict
            ``EdgarClient.get_company_facts()`` output (or a recorded snapshot).
        ticker : str
            Company ticker.
        period : str | None
            Fiscal year to extract (``"2025"``, ``"FY2025"``, or an exact
            year-end ``"2025-09-27"``). ``None`` = the latest fiscal year with
            reported annual revenue.
        source : str
            Recorded as ``provenance["source"]``.
        """
        series = {
            concept: extract_financial_series(facts, concept, unit=unit)
            for chain in self.XBRL_CONCEPTS.values()
            for concept, unit in chain
        }
        snapshot = facts.get("_snapshot") if isinstance(facts, dict) else None
        return self.extract_from_xbrl(series, ticker, period, source=source,
                                      snapshot=snapshot if isinstance(snapshot, dict) else None)

    def extract_from_xbrl(
        self,
        xbrl_series: dict[str, list[dict]],
        ticker: str,
        period: str | None = None,
        source: str = "SEC EDGAR XBRL companyfacts",
        snapshot: dict | None = None,
    ) -> ExtractedKPIs:
        """
        Extract one fiscal year's KPIs from pre-parsed XBRL series.

        ``snapshot`` is the ``_snapshot`` block of a bundled payload: its
        ``not_carried`` concepts were never fetched, so their absence is
        reported as unknown rather than as the filer's.

        The fiscal year is anchored on annual revenue: ``period`` selects it
        (``"2025"`` / ``"FY2025"`` by fiscal-year label, or an exact
        ``"YYYY-MM-DD"`` year-end); ``None`` takes the latest one reported.
        Every other figure is read at exactly that year-end — a concept with
        no value there is left ``None`` and listed in ``provenance["missing"]``
        rather than filled from another year.

        Parameters
        ----------
        xbrl_series : dict[str, list[dict]]
            Mapping of concept name → list of {end, val, ...} entries, as
            returned by ``EdgarClient.extract_financial_series``.
        ticker : str
            Company ticker.
        period : str | None
            Target fiscal year (see above).
        source : str
            Recorded as ``provenance["source"]``.

        Returns
        -------
        ExtractedKPIs
            ``period`` is the fiscal-year label (e.g. ``"FY2025"``) and
            ``provenance`` names the fiscal year-end, filing, and concept (or
            formula) behind every figure.

        Raises
        ------
        ValueError
            If no annual revenue is reported (nothing to anchor a year on), or
            the requested fiscal year is not in the data.
        """
        fy_end, fy_label, anchor = self._select_fiscal_year(xbrl_series, period)
        kpis = ExtractedKPIs(
            ticker=ticker,
            period=f"FY{fy_label}",
            source_type="10-K",
            extraction_method="xbrl",
        )
        concepts: dict[str, str] = {}
        # Which filing each concept's value was read from: the LATEST 10-K that
        # reported it for this year-end (a figure re-reported in a later 10-K wins; a
        # 10-K/A is never read, see extract_financial_series). For an older year
        # that is often a later 10-K than the year's own; a balance-sheet value
        # appears only in the two 10-Ks that show that date.
        filings: dict[str, dict[str, str | None]] = {}

        def pick(field_name: str) -> float | None:
            for concept, unit in self.XBRL_CONCEPTS[field_name]:
                for e in xbrl_series.get(concept, []):
                    if e.get("end") == fy_end and e.get("unit", unit) == unit:
                        concepts[field_name] = concept
                        filings[concept] = {"accession": e.get("accn"), "filed": e.get("filed")}
                        scale = 1.0 if unit == "USD/shares" else 1e6  # → $M / millions
                        return float(e["val"]) / scale
            return None

        revenue = pick("revenue")
        kpis.revenue = revenue

        gross_profit = pick("gross_profit")
        if gross_profit is None:
            cost = pick("cost_of_revenue")
            if revenue is not None and cost is not None:
                gross_profit = revenue - cost
                concepts["gross_profit"] = (
                    f"{concepts['revenue']} − {concepts.pop('cost_of_revenue')} (derived)"
                )
        else:
            concepts.pop("cost_of_revenue", None)
        kpis.gross_profit = gross_profit

        kpis.operating_income = pick("operating_income")
        kpis.net_income = pick("net_income")

        # EBITDA is almost never tagged in us-gaap; derive it as operating
        # income + D&A rather than letting the DCF fall back to a default margin.
        # D&A is the filer's TOTAL concept when it reports one; otherwise
        # Depreciation + AmortizationOfIntangibleAssets; otherwise Depreciation
        # alone — property and equipment only, which understates D&A and EBITDA
        # and is listed in provenance["limitations"].
        limitations: dict[str, str] = {}
        # Concepts this extraction reads that the bundled snapshot never
        # fetched: their absence says nothing about the filer.
        not_carried = sorted(set((snapshot or {}).get("not_carried") or [])
                             & {c for chain in self.XBRL_CONCEPTS.values() for c, _ in chain})
        cut = (snapshot or {}).get("retrieved", "")
        da = pick("depreciation_amortization")
        if da is not None:
            da_formula = concepts.pop("depreciation_amortization")
        else:
            dep = pick("depreciation_only")
            amort = pick("amortization_of_intangibles")
            da_formula = None
            if dep is not None and amort is not None:
                da = dep + amort
                da_formula = (f"({concepts.pop('depreciation_only')} + "
                              f"{concepts.pop('amortization_of_intangibles')})")
            elif dep is not None:
                da = dep
                da_formula = concepts.pop("depreciation_only")
                concepts.pop("amortization_of_intangibles", None)
                amort_note = (
                    f"AmortizationOfIntangibleAssets is not in the bundled snapshot "
                    f"(cut {cut}, before FinSight read it), so whether the filer "
                    "reports it is unknown until a refresh"
                    if "AmortizationOfIntangibleAssets" in not_carried else
                    "nor AmortizationOfIntangibleAssets"
                )
                limitations["depreciation_amortization"] = (
                    "D&A is us-gaap:Depreciation — depreciation of property and "
                    "equipment only. The filer's 10-K facts for this fiscal year report "
                    "no total D&A concept (DepreciationDepletionAndAmortization, "
                    "DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet)"
                    + ("; " if not_carried else " ") + amort_note
                    + ". D&A and EBITDA exclude any amortization and are understated."
                )
        if kpis.operating_income is not None and da is not None:
            kpis.ebitda = kpis.operating_income + da
            concepts["ebitda"] = f"OperatingIncomeLoss + {da_formula} (derived)"
        # PP&E depreciation and acquired-intangible amortization, each as
        # reported (None when not tagged). Total D&A is the right non-cash
        # add-back, but only the PP&E part is replaced by CapEx: the DCF's
        # steady-state CapEx uses these to leave acquisition amortization out.
        kpis.depreciation = pick("depreciation_only")
        if kpis.depreciation is not None:
            concepts["depreciation"] = concepts.pop("depreciation_only")
        kpis.amortization_of_intangibles = pick("amortization_of_intangibles")
        # Always true of the concepts read: finance leases are not in either.
        limitations["finance_leases"] = (
            "CapEx is cash paid for property and equipment "
            f"({{capex_concept}}); assets obtained under finance leases are not "
            "included, and finance-lease liabilities are not in total debt."
        )

        # Free cash flow = operating cash flow − capital expenditure, and only
        # when BOTH are reported: CFO alone would overstate FCF by the capex.
        cfo = pick("operating_cash_flow")
        kpis.capex = pick("capex")
        limitations["finance_leases"] = limitations["finance_leases"].replace(
            "{capex_concept}", concepts.get("capex", "no CapEx concept reported"))
        if cfo is not None and kpis.capex is not None:
            kpis.free_cash_flow = cfo - kpis.capex
            concepts["free_cash_flow"] = (
                f"{concepts.pop('operating_cash_flow')} − {concepts['capex']} (derived)"
            )
        else:
            concepts.pop("operating_cash_flow", None)

        # Total debt = long-term debt + short-term borrowings / commercial paper.
        # LongTermDebt usually holds the current and noncurrent parts, but not for
        # every filer: Tesla tags only the noncurrent part (FY2025: $6,584M of it,
        # beside $1,569M current). See _ltd_excludes_current: current debt is added
        # only on evidence.
        # Every debt figure read goes in `debt_seen`, used or not: what is reported
        # above the total says the total cannot be holding it.
        debt_seen: dict[str, float] = {}

        def take(name: str) -> tuple[float | None, str | None]:
            value = pick(name)
            concept = concepts.pop(name, None)
            if value is not None and not name.startswith("finance_leases"):
                debt_seen[concept] = value
            return value, concept

        ltd, ltd_concept = take("long_term_debt")
        noncurrent, noncurrent_concept = take("long_term_debt_noncurrent")
        current = _current_debt(take)
        short, short_concept = take("short_term_debt")
        excludes = (None if ltd is None else _ltd_excludes_current(
            ltd, noncurrent, current.value, take("debt_and_leases_current")[0],
            take("debt_and_leases_noncurrent")[0], take("finance_leases_current")[0],
            take("finance_leases_noncurrent")[0]))

        parts: list[tuple[str, float]] = []
        wider = _WiderDebt()
        if ltd is not None:
            parts.append((ltd_concept, ltd))
            if excludes:
                parts.append((current.concept, current.value))
        else:
            if noncurrent is not None:
                parts.append((noncurrent_concept, noncurrent))
            else:
                # Without those two, short-term debt alone would make $20B of SeniorNotes
                # beside $1B of CommercialPaper read as $1B.
                wider = _wider_long_term_debt(take, current)
                parts.extend(wider.parts)
            if current.value is not None and not wider.holds_current:
                parts.append((current.concept, current.value))
        current_counted = any(c == current.concept for c, _ in parts)
        if short is not None and not (current_counted and current.holds_short_term):
            parts.append((short_concept, short))
        if parts:
            kpis.total_debt = sum(v for _, v in parts)
            concepts["total_debt"] = " + ".join(c for c, _ in parts) + (
                " (derived)" if len(parts) > 1 else ""
            )
        note = _current_debt_note(
            ltd, noncurrent, current, excludes, current_counted, fy_end,
            carried=snapshot is None or "DebtCurrent" not in not_carried, wider=wider)
        if note:
            limitations["current_debt"] = note
        if parts and ltd is None and noncurrent is None and not wider.long_term:
            limitations["long_term_debt_not_found"] = (
                f"Long-term debt was not found: {concepts['total_debt']} is reported for {fy_end}, "
                "but no long-term debt concept FinSight reads"
                + (" is in this bundled snapshot, which does not carry them all" if not_carried
                   else " is")
                + f" ({_LONG_TERM_DEBT_CONCEPTS}). Total debt is that short-term figure alone. The "
                "list holds common tags, not every tag, so long-term debt under another one is "
                "missing and the per-share value overstated by all of it."
            )
        if wider.leases_inside:
            limitations["long_term_debt_leases"] = (
                f"Long-term debt is {wider.leases_inside}, the balance sheet's line: the "
                "FinanceLeaseLiability tags that would take finance leases out of it are not all "
                "reported, so any noncurrent finance leases are counted in total debt here."
            )
        if wider.leases_inside or (current_counted and current.holds_leases):
            limitations["finance_leases"] = limitations["finance_leases"].replace(
                "are not in total debt.",
                "are not in total debt, except where a note on debt here says otherwise.")
        if parts:
            for field_name in _WIDER_DEBT_FIELDS:
                take(field_name)
            if ltd is not None and noncurrent is not None and current.value is not None:
                debt_seen[f"{noncurrent_concept} + {current.concept}"] = noncurrent + current.value
            leases = sum(v for v, _ in (take("finance_leases_current"),
                                        take("finance_leases_noncurrent")) if v is not None)
            above = _debt_above_total_note(
                kpis.total_debt, concepts["total_debt"], debt_seen, fy_end, leases)
            if above:
                limitations["debt_above_total"] = above

        kpis.cash_and_equivalents = pick("cash_and_equivalents")
        kpis.r_and_d_expense = pick("r_and_d_expense")
        kpis.eps_diluted = pick("eps_diluted")

        # Per-share value needs DILUTED shares: the fiscal year's diluted
        # weighted-average count, else the period-end basic count.
        shares = pick("diluted_shares")
        period_end_above_diluted = None
        if shares is not None:
            diluted_filing = filings[concepts["diluted_shares"]]
            concepts["shares_outstanding"] = concepts.pop("diluted_shares")
            # A weighted average can sit below the year-end count, and the DCF says what
            # that moves. Only within one filing: two filings can straddle a stock split.
            period_end = pick("period_end_shares")
            period_end_concept = concepts.pop("period_end_shares", None)
            if (period_end is not None and period_end > shares
                    and filings[period_end_concept] == diluted_filing):
                period_end_above_diluted = period_end
        else:
            shares = pick("period_end_shares")
            if shares is not None:
                concepts["shares_outstanding"] = concepts.pop("period_end_shares")
        kpis.shares_outstanding = shares
        split_note = _share_basis_note(
            xbrl_series.get(concepts.get("shares_outstanding", ""), []), fy_end)
        if split_note:
            limitations["stock_split_basis"] = split_note

        if kpis.revenue and kpis.revenue > 0:
            if kpis.gross_profit is not None:
                kpis.gross_margin = kpis.gross_profit / kpis.revenue
            if kpis.operating_income is not None:
                kpis.operating_margin = kpis.operating_income / kpis.revenue
            if kpis.net_income is not None:
                kpis.net_margin = kpis.net_income / kpis.revenue

        kpis.revenue_growth_yoy, prior = self._revenue_growth_yoy(
            xbrl_series, fy_end, concepts.get("revenue")
        )
        if prior is not None:
            concepts["revenue_growth_yoy"] = (
                f"{concepts['revenue']} vs fiscal year ended {prior} (derived)"
            )

        # The fiscal year's OWN 10-K: the earliest filing that reported its revenue,
        # not a later one re-reporting it, which need not contain the year's other figures.
        accession = anchor.get("first_accn") or anchor.get("accn")
        filed = anchor.get("first_filed") or anchor.get("filed")
        used = {c for c in filings if c in set(_concepts_named(concepts))}
        share_filing = filings.get(concepts.get("shares_outstanding", ""), {})
        kpis.provenance = {
            "source": source,
            "form": "10-K",
            "fiscal_year": kpis.period,
            "fiscal_year_end": fy_end,
            "accession": accession,
            "filed": filed,
            "figure_filings": {c: filings[c] for c in sorted(used)},
            # Share counts and EPS are as that filing reported them. A stock
            # split after it is NOT applied: NVDA's FY2022 diluted count is
            # 2,535M (pre 10:1 split, from its 2024-02-21 10-K) and FY2023's
            # 25,070M (post split, from its 2025-02-26 10-K).
            "per_share_basis": (
                f"as reported in the 10-K filed {share_filing.get('filed')} "
                f"({share_filing.get('accession')}); not adjusted for any stock "
                "split after that filing"
            ) if share_filing else None,
            # The filing date the share count is "as of" — a stock split after
            # it puts the per-share value and a split-adjusted price on
            # different bases (checked by the API and the dashboard).
            "per_share_filed": share_filing.get("filed") if share_filing else None,
            # Year-end shares (CommonStockSharesOutstanding, same 10-K), only when above
            # the diluted weighted-average count used; DCFModel discloses the effect.
            "period_end_shares_above_diluted": period_end_above_diluted,
            "concepts": {k: concepts[k] for k in sorted(concepts)},
            # What the figures above cannot show, by field.
            "limitations": dict(sorted(limitations.items())),
            # Concepts read that this bundled snapshot does not carry (absent
            # for live data): unknown, not "not reported".
            "not_in_snapshot": not_carried,
            "missing": [f for f in self._REPORTED_FIELDS if getattr(kpis, f) is None],
        }
        return kpis

    def _select_fiscal_year(
        self,
        xbrl_series: dict[str, list[dict]],
        period: str | None,
    ) -> tuple[str, str, dict]:
        """Return ``(fiscal_year_end, fiscal_year_label, anchor_entry)``.

        A year-end's label is the ``fy`` of the earliest 10-K that reported it
        (later 10-Ks re-report old periods under their own ``fy``); entries
        without filing metadata fall back to the year-end's calendar year.
        """
        by_end: dict[str, dict] = {}
        for concept, _unit in self.XBRL_CONCEPTS["revenue"]:
            for e in xbrl_series.get(concept, []):
                end = e.get("end")
                if not end:
                    continue
                first = e.get("first_filed") or e.get("filed") or ""
                held = by_end.get(end)
                if held is None or first < held["_first"]:
                    by_end[end] = {**e, "_first": first}
        if not by_end:
            raise ValueError(
                "No annual revenue is reported in the 10-K XBRL data, so no "
                "fiscal year can be anchored."
            )

        # A filing's `fy` names ITS fiscal year, i.e. its latest period. A
        # company's first XBRL 10-K also reports two comparative years that no
        # earlier filing did, so labelling a year by "the fy of the first 10-K
        # that reported it" gave all three that filing's year (FY2021 ×3).
        # Each period is labelled from its own end date, shifted by the
        # filing's own fy-vs-end-year offset (a January/February year-end
        # filer may name a year one behind its end date).
        own: dict[str, tuple[str, object]] = {}
        for e in by_end.values():
            accn, fy = e.get("first_accn"), e.get("first_fy")
            if accn and fy and (accn not in own or e["end"] > own[accn][0]):
                own[accn] = (e["end"], fy)

        def label(e: dict) -> str:
            accn = e.get("first_accn")
            if accn in own:
                own_end, own_fy = own[accn]
                try:
                    return str(int(e["end"][:4]) + int(own_fy) - int(own_end[:4]))
                except (TypeError, ValueError):
                    pass
            fy = e.get("first_fy") or (None if e.get("first_filed") else e.get("fy"))
            return str(fy) if fy else e["end"][:4]

        raw = (period or "").strip()
        wanted = raw.upper().removeprefix("FY")
        if raw and not wanted:
            # "FY" alone must not fall through to the latest year.
            raise ValueError("period 'FY' names no fiscal year; pass e.g. FY2025, or omit it.")
        if not wanted:
            end = max(by_end)
        elif len(wanted) == 10 and wanted[4] == "-":
            if wanted not in by_end:
                raise ValueError(f"No annual 10-K revenue reported for a fiscal year ending {wanted}.")
            end = wanted
        else:
            matches = [e for e, v in by_end.items() if label(v) == wanted]
            if not matches:
                available = ", ".join(f"FY{label(by_end[e])}" for e in sorted(by_end)[-4:])
                raise ValueError(
                    f"No annual 10-K revenue reported for fiscal year {wanted} "
                    f"(latest available: {available})."
                )
            end = max(matches)
        anchor = by_end[end]
        return end, label(anchor), anchor

    def _revenue_growth_yoy(
        self,
        xbrl_series: dict[str, list[dict]],
        fy_end: str,
        revenue_concept: str | None,
    ) -> tuple[float | None, str | None]:
        """Revenue growth for the fiscal year ending ``fy_end``, vs the year before.

        Like is compared with like: the prior year is read from the same
        concept as this year's revenue where it exists (falling back down the
        revenue chain only for a filer that switched concepts), and the two
        year-ends must be one fiscal year apart. Normalisation and the growth
        math come from :class:`FinancialParser`.

        Returns ``(growth, prior_fiscal_year_end)``; ``(None, None)`` when no
        consecutive prior year is reported.
        """
        if revenue_concept is None:
            return None, None
        chain = [c for c, _ in self.XBRL_CONCEPTS["revenue"]]
        chain = [revenue_concept] + [c for c in chain if c != revenue_concept]

        parser = FinancialParser()
        df = parser.normalize_xbrl_to_df(xbrl_series, chain, period_type="annual")
        if df.empty:
            return None, None
        merged = None
        for concept in chain:
            if concept in df.columns:
                merged = df[concept] if merged is None else merged.combine_first(df[concept])
        series = merged.dropna()
        series = series[series.index <= pd.Timestamp(fy_end)]
        if len(series) < 2 or series.index[-1] != pd.Timestamp(fy_end):
            return None, None
        gap = (series.index[-1] - series.index[-2]).days
        if not ANNUAL_MIN_DAYS <= gap <= ANNUAL_MAX_DAYS:
            return None, None
        growth = parser.compute_growth_rates(series.iloc[-2:]).get("yoy_1y")
        return growth, series.index[-2].date().isoformat()

    # ------------------------------------------------------------------
    # private: LLM extraction
    # ------------------------------------------------------------------

    def _extract_llm(
        self, text: str, ticker: str, period: str, source_type: str
    ) -> ExtractedKPIs:
        try:
            prompt = _KPI_EXTRACTION_PROMPT.format(text=text)
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            data = json.loads(raw)
            return self._dict_to_kpis(data, ticker, period, source_type, method="llm")
        except Exception as e:
            logger.error(f"LLM extraction failed: {e}. Falling back to regex.")
            return self._extract_regex(text, ticker, period, source_type)

    # ------------------------------------------------------------------
    # private: regex extraction
    # ------------------------------------------------------------------

    def _extract_regex(
        self, text: str, ticker: str, period: str, source_type: str
    ) -> ExtractedKPIs:
        """Basic regex extraction for common financial patterns."""
        kpis = ExtractedKPIs(
            ticker=ticker,
            period=period,
            source_type=source_type,
            extraction_method="regex",
        )

        def money(found: re.Match) -> float:
            """A ``$<number><unit>`` figure in $M.

            The unit may be a word or a suffix — trillion/tn/T, billion/bn/B,
            million/mn/mm/M, thousand/K — and is applied as written. A figure with no unit is taken as
            $M (the convention of the LLM path), never guessed from its size.
            """
            number = float(found.group("number").replace(",", ""))
            return number * _UNIT_TO_MILLIONS[(found.group("unit") or "").lower()]

        def percent(found: re.Match) -> float:
            return float(found.group("number")) / 100

        # The fallback reads these six fields only (REGEX_FIELDS); the LLM
        # path asks for the 22 in _KPI_EXTRACTION_PROMPT. "Cost of revenue" and
        # "deferred revenue" are not revenue.
        kpis.revenue = _read_level(
            text,
            r"\b(?<!cost of )(?<!costs of )(?<!deferred )(?<!unearned )(?:revenue|net\s+sales)",
            _MONEY_FIGURE, money, can_lose=False)
        kpis.net_income = _read_level(text, r"\bnet\s+(?:income|loss(?:es)?)\b", _MONEY_FIGURE, money)
        kpis.ebitda = _read_level(text, r"\bebitda\b", _MONEY_FIGURE, money)
        kpis.free_cash_flow = _read_level(text, r"\bfree\s+cash\s+flow\b", _MONEY_FIGURE, money)
        kpis.gross_margin = _read_level(text, r"\bgross\s+margin\b", _PERCENT_FIGURE, percent)
        kpis.operating_margin = _read_level(text, r"\boperating\s+margin\b", _PERCENT_FIGURE, percent)

        return kpis

    def _dict_to_kpis(
        self, data: dict, ticker: str, period: str, source_type: str, method: str
    ) -> ExtractedKPIs:
        kpis = ExtractedKPIs(ticker=ticker, period=period, source_type=source_type,
                             extraction_method=method)
        fields = [f.name for f in kpis.__dataclass_fields__.values()
                  if f.name not in ("ticker", "period", "source_type", "extraction_method")]
        # Coerce every value to its field's type: an LLM may answer "$10.5B", "15%" or a
        # list where a number belongs. Unusable values become None.
        for field_name in fields:
            value = data.get(field_name)
            if value is None:
                continue
            if field_name in ("key_risks", "growth_drivers"):
                setattr(kpis, field_name,
                        [str(v) for v in value] if isinstance(value, list) else [])
            elif field_name == "guidance_period":
                p = str(value).strip().lower()
                kpis.guidance_period = p if p in ("annual", "quarterly") else None
            else:
                setattr(kpis, field_name, _as_number(value, field_name))
        return kpis


# Within this share of each other, two debt figures are the same figure. Carrying
# amounts reconcile to the dollar; a filer's rounding and a discount do not reach 1%.
_DEBT_RECONCILES_WITHIN = 0.01


def _debt_close(a: float, b: float) -> bool:
    return abs(a - b) <= _DEBT_RECONCILES_WITHIN * max(abs(a), abs(b))


@dataclass(frozen=True)
class _CurrentDebt:
    """Debt due within a year at the fiscal year-end, however the filer tags it."""

    value: float | None = None
    concept: str | None = None
    holds_short_term: bool = False
    """DebtCurrent includes short-term borrowings: adding them to it counts them twice."""
    holds_leases: bool = False
    """The balance-sheet line, with no finance-lease tag to take out of it."""


def _current_debt(take) -> _CurrentDebt:
    """The narrowest current-debt concept the filer reports (``take`` reads one).
    NotesPayableCurrent comes last: it covers notes, not all current debt."""
    value, concept = take("long_term_debt_current")
    if value is not None:
        return _CurrentDebt(value, concept)
    value, concept = take("debt_current")
    if value is not None:
        return _CurrentDebt(value, concept, holds_short_term=True)
    value, concept = take("debt_and_leases_current")
    if value is None:
        value, concept = take("notes_current")
        return _CurrentDebt(value, concept) if value is not None else _CurrentDebt()
    # Read again by the reconciliation: take() leaves the fact where it was.
    leases, lease_concept = take("finance_leases_current")
    if leases is None:
        return _CurrentDebt(value, concept, holds_leases=True)
    return _CurrentDebt(value - leases, f"({concept} − {lease_concept})")


def _ltd_excludes_current(
    ltd: float,
    noncurrent: float | None,
    current: float | None,
    sheet_current: float | None,
    sheet_noncurrent: float | None,
    leases_current: float | None,
    leases_noncurrent: float | None,
) -> bool | None:
    """Whether ``LongTermDebt`` leaves current debt out: True, False, or None
    when nothing here shows which. Current debt is added only on True, so a
    filer whose LongTermDebt already holds it is never counted twice.

    True takes one of two pieces of evidence. LongTermDebt equals
    LongTermDebtNoncurrent while current debt is more than rounding beside it; or
    LongTermDebt alone misses the balance sheet's debt and LongTermDebt + current
    meets it. The sheet's two lines include finance leases, taken out when tagged.
    """
    if noncurrent is not None:
        if not _debt_close(ltd, noncurrent):
            return False    # LongTermDebt is more than the noncurrent part.
        if current is None:
            return None     # The noncurrent part, and no current figure to go with it.
        return current != 0 and not _debt_close(ltd, noncurrent + current)
    if not current:
        return None if current is None else False    # Reported 0: nothing to leave out.
    if sheet_current is None or sheet_noncurrent is None:
        return None
    debt_on_sheet = sheet_current + sheet_noncurrent - (leases_current or 0) - (leases_noncurrent or 0)
    if _debt_close(ltd, debt_on_sheet):
        return False
    return True if _debt_close(ltd + current, debt_on_sheet) else None


_CURRENT_DEBT_CONCEPTS = ("LongTermDebtCurrent, DebtCurrent, "
                          "LongTermDebtAndCapitalLeaseObligationsCurrent, NotesPayableCurrent")
_LONG_TERM_DEBT_CONCEPTS = (
    "LongTermDebt, LongTermDebtNoncurrent, LongTermDebtAndCapitalLeaseObligations and its "
    "IncludingCurrentMaturities total, NotesPayable, LongTermNotesPayable, "
    "NotesPayableNoncurrent, SeniorNotes, SeniorLongTermNotes, SeniorNotesNoncurrent, "
    "ConvertibleNotesPayable, ConvertibleLongTermNotesPayable, ConvertibleNotesPayableNoncurrent")
_WIDER_DEBT_FIELDS = (
    "debt_and_leases_total", "debt_and_leases_noncurrent",
    "notes_total", "notes_noncurrent", "notes_current",
    "senior_notes_total", "senior_notes_noncurrent", "senior_notes_current",
    "convertible_notes_total", "convertible_notes_noncurrent", "convertible_notes_current",
)


@dataclass(frozen=True)
class _WiderDebt:
    """Long-term debt from the wider tags, for a filer with neither LongTermDebt nor
    LongTermDebtNoncurrent."""

    parts: tuple[tuple[str, float], ...] = ()
    """What goes into total debt: the long-term figures and any current form of their own."""
    long_term: tuple[str, ...] = ()
    """The concepts among ``parts`` that are long-term debt."""
    holds_current: bool = False
    """A total concept: its current part is inside it, so current debt is not added to it."""
    current_seen: bool = False
    """A current form of these tags is reported, so current debt is not simply absent."""
    current_held: bool = False
    """The current figure is the total's own line: its name says the total holds it."""
    leases_inside: str | None = None
    """The balance-sheet line, with finance leases in it that no tag can take out."""


def _wider_long_term_debt(take, current: _CurrentDebt) -> _WiderDebt:
    """Long-term debt from the common tags beyond LongTermDebt. A total wins over its parts.

    In order: the balance sheet's lines, which are debt AND finance leases, so the leases are
    taken out when tagged; then notes payable, the whole of the notes; then senior and
    convertible notes, two classes of them that add up. Within each: the total concept, else
    its noncurrent form. A class's current form is inside its own total, and is left out
    when the filer's broader current concept (``current``) is there to hold it.
    """
    leases_current = take("finance_leases_current")
    leases_noncurrent = take("finance_leases_noncurrent")

    def leases_out(line: tuple[float, str], *leases: tuple) -> tuple[str, float, str | None]:
        tagged = [lease for lease in leases if lease[0] is not None]
        if not tagged:
            return line[1], line[0], line[1]
        named = " − ".join([line[1], *(c for _, c in tagged)])
        return (f"({named})", line[0] - sum(v for v, _ in tagged),
                line[1] if len(tagged) < len(leases) else None)

    line = take("debt_and_leases_total")
    if line[0] is not None:
        concept, value, inside = leases_out(line, leases_current, leases_noncurrent)
        return _WiderDebt(((concept, value),), (concept,), holds_current=True, current_seen=True,
                          current_held="ObligationsCurrent" in (current.concept or ""),
                          leases_inside=inside)
    line = take("debt_and_leases_noncurrent")
    if line[0] is not None:
        concept, value, inside = leases_out(line, leases_noncurrent)
        return _WiderDebt(((concept, value),), (concept,), leases_inside=inside)

    value, concept = take("notes_total")
    if value is not None:
        return _WiderDebt(((concept, value),), (concept,), holds_current=True,
                          current_held=current.concept == "NotesPayableCurrent")
    classes = [(take(f"{name}_total"), take(f"{name}_noncurrent"), take(f"{name}_current"))
               for name in ("senior_notes", "convertible_notes")]
    notes = take("notes_noncurrent")
    if notes[0] is not None:
        long_term, holds_current = [notes], False
    else:
        long_term = [total if total[0] is not None else noncurrent
                     for total, noncurrent, _ in classes
                     if total[0] is not None or noncurrent[0] is not None]
        holds_current = any(total[0] is not None for total, _, _ in classes)
    own_current = [] if current.value is not None else [
        form for total, _, form in classes if form[0] is not None and total[0] is None]
    return _WiderDebt(
        tuple((c, v) for v, c in long_term + own_current), tuple(c for _, c in long_term),
        holds_current=holds_current,
        current_seen=any(form[0] is not None for _, _, form in classes))


def _current_debt_note(
    ltd: float | None,
    noncurrent: float | None,
    current: _CurrentDebt,
    excludes: bool | None,
    current_counted: bool,
    fy_end: str,
    carried: bool,
    wider: _WiderDebt = _WiderDebt(),
) -> str | None:
    """What total debt cannot show about current debt, or None when it is settled."""
    if current_counted and current.holds_leases:
        return (f"Current debt is {current.concept}, the balance sheet's line: no "
                "FinanceLeaseLiabilityCurrent is reported to take out of it, so any current "
                "finance leases are counted in total debt here.")
    if ltd is None and noncurrent is None and not wider.long_term:
        if current.value is None:
            return None
        return (f"Only current debt is reported for {fy_end} ({current.concept}): no "
                "LongTermDebt or LongTermDebtNoncurrent. Total debt is that alone, so any "
                "noncurrent debt tagged otherwise is missing and the value overstated by it.")
    effect = ("If it does, net debt is understated and the per-share value overstated "
              "by the current debt left out.")
    which = ("LongTermDebt" if ltd is not None else
             "LongTermDebtNoncurrent" if noncurrent is not None else " + ".join(wider.long_term))
    if current.value is None:
        if wider.current_seen:
            return None
        if ltd is None or excludes is None:
            lead = (f"Unreconciled: current debt not in this snapshot. It holds no "
                    f"{_CURRENT_DEBT_CONCEPTS} fact for {fy_end}, and nothing it carries "
                    f"reconciles {which}, so whether the filer's total debt holds its current "
                    "part is unknown until a refresh."
                    if not carried else
                    f"No current-debt concept ({_CURRENT_DEBT_CONCEPTS}) is reported for "
                    f"{fy_end} and nothing reconciles {which}.")
            return f"{lead} Total debt is {which} as tagged, which may leave current debt out. {effect}"
        return None
    if (excludes is None and ltd is not None) or (
            wider.holds_current and not current_counted and not wider.current_held):
        return (f"{current.concept} (${current.value:,.0f}M) is reported for {fy_end}, but nothing "
                f"here shows whether {which} already holds it, so it is not added. If it does "
                "not, net debt is understated and the per-share value overstated by that amount.")
    return None


def _debt_above_total_note(
    total: float, formula: str, seen: dict[str, float], fy_end: str, leases: float = 0.0
) -> str | None:
    """A debt figure the filer reports above total debt: the total cannot be holding it.

    The chain's rule stands (a total concept wins, and nothing is added without evidence), so
    the figure is not added. It is said: LongTermDebt of $1B beside SeniorNotes of $20B has
    one of them wrong for the question "how much debt", and nothing here shows which. A
    figure the total was made from is not compared, and the balance sheet's lines are
    compared without the finance leases tagged inside them.
    """
    used = set(re.findall(r"[A-Z][A-Za-z]+", formula))

    def debt_in(concept: str, value: float) -> float:
        return value - leases if concept.startswith("LongTermDebtAndCapitalLease") else value

    above = sorted((v, c) for c, v in seen.items() if c not in used
                   and debt_in(c, v) > total and not _debt_close(debt_in(c, v), total))
    if not above:
        return None
    value, concept = above[-1]
    return (f"{concept} (${value:,.0f}M) is reported for {fy_end}, above the ${total:,.0f}M of "
            f"total debt ({formula}), so that total cannot be holding all of it. Nothing here "
            "shows how the two overlap, so it is not added. If it is debt on top, net debt is "
            "understated and the per-share value overstated by up to the difference, "
            f"${value - total:,.0f}M.")


# Share counts this far apart between two filings look like a split: 3:2, the smallest
# common ratio, less what a year's buybacks take off it.
_SPLIT_LIKE_RATIO = 1.4


def _share_basis_note(rows: list[dict], fy_end: str) -> str | None:
    """Why this year's per-share value is not comparable with a neighbouring year's.

    Each count is as its own 10-K reported it, so two filings can straddle a stock split:
    NVIDIA's year to 2022-01-30 reads 2,535M from a 10-K before its 10:1 split, the next
    year 25,070M from one after it, and their values $94.13 and $1.50. Only across two
    filings: one 10-K states all its years on one basis, so a jump inside it is real.
    """
    rows = sorted((r for r in rows if r.get("end") and r.get("val")), key=lambda r: r["end"])
    here = next((i for i, r in enumerate(rows) if r["end"] == fy_end), None)
    if here is None:
        return None
    this = rows[here]
    jumps = []
    for other in rows[max(here - 1, 0):here] + rows[here + 1:here + 2]:
        gap = abs((pd.Timestamp(other["end"]) - pd.Timestamp(fy_end)).days)
        ratio = max(other["val"], this["val"]) / min(other["val"], this["val"])
        if (other.get("accn") != this.get("accn") and ANNUAL_MIN_DAYS <= gap <= ANNUAL_MAX_DAYS
                and ratio >= _SPLIT_LIKE_RATIO):
            jumps.append(f"{other['val'] / 1e6:,.0f}M for the fiscal year ended {other['end']} "
                         f"(10-K filed {other.get('filed')})")
    if not jumps:
        return None
    return (f"The share count is as its 10-K reported it, not adjusted for a split after that "
            f"filing: {this['val'] / 1e6:,.0f}M here (10-K filed {this.get('filed')}) against "
            f"{' and '.join(jumps)}. A jump that size between two filings is what a stock split "
            "looks like, so this year's per-share value and EPS are not comparable with that "
            "year's; enterprise and equity value are.")


def _concepts_named(concepts: dict[str, str]) -> list[str]:
    """Every us-gaap concept name appearing in the provenance concept strings
    (derived entries read "A − B (derived)")."""
    return [tok for v in concepts.values() for tok in re.findall(r"[A-Z][A-Za-z]+", v)]


def _as_number(value, field_name: str) -> float | None:
    """An LLM-returned figure as a float ($M, or a decimal ratio), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") else None
    if not isinstance(value, str):
        return None
    m = re.fullmatch(rf"\s*\$?\s*(-?[\d,]*\.?\d+)\s*({_UNIT_WORDS}|%)?\s*", value, re.IGNORECASE)
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit == "%":
        return number / 100
    if field_name in ("eps_diluted",) or field_name.endswith(("_margin", "_yoy")):
        return number
    return number * _UNIT_TO_MILLIONS[unit]

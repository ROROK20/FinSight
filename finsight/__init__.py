"""
FinSight — automated equity research from SEC filings.

Reads one fiscal year of XBRL financials from SEC EDGAR 10-Ks with every figure
cited, builds a DCF from them, and generates research reports. Comparable-company
multiples come from Yahoo Finance, not filings. Sentiment scores earnings-call
text, not filings; the API's only transcript source is a third-party page, so
its results are tagged ``synthetic`` when they fall back to the placeholder
transcript, as they do today. ``KPIExtractor.extract_from_text``
optionally uses an LLM on unstructured text; the API uses XBRL only.
"""

__version__ = "0.1.0"
__author__ = "Rohan Khiani"
__license__ = "MIT"

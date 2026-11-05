# Usage Guide

Concrete, copy-paste examples for using FinSight as a Python library and over
its REST API. All snippets are grounded in the actual public API of the
`finsight` package.

> **Setup:** `pip install -e .` (add `".[llm]"` for LLM extraction,
> `".[finbert]"` for deep-learning sentiment). For live SEC requests, set
> `SEC_EDGAR_USER_AGENT` (see [`.env.example`](../.env.example)).

---

## 1. Fetch data from SEC EDGAR

The SEC APIs are free and require no key — only a `User-Agent` naming you.
Set `SEC_EDGAR_USER_AGENT` to your own name and email first — in the form
`"Jane Doe jane.doe@example.org"`, with your details (that example address is
itself refused). Without a deliverable contact every live request raises
`SECUserAgentError`; there is no fallback identity.

```python
from finsight.ingestion.edgar import EdgarClient

client = EdgarClient()

# Resolve a ticker to its zero-padded CIK
cik = client.get_cik("AAPL")            # → "0000320193"

# List recent 10-K filings
filings = client.get_filings(cik, form_type="10-K", limit=5)
for f in filings:
    print(f.period_of_report, f.filing_date, f.url)

# Download the full filing text (truncated to max_chars)
text = client.get_filing_text(filings[0])

# Fetch XBRL structured facts (the most reliable source of financials)
facts = client.get_company_facts(cik)
# One entry per fiscal year, in the concept's own unit. In SEC's live data Apple's
# `Revenues` stops in FY2018, before the bundled snapshot's window (so the bundled
# AAPL file has none); its revenue is the ASC 606 concept since.
revenue_series = client.extract_financial_series(
    facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
# → [..., {'end': '2026-09-26', 'val': 478834000000, 'fy': 2026, 'form': '10-K', ...}]
#   (SEC data as of 2026-11-04)
```

---

## 2. Extract KPIs

### From XBRL (recommended — deterministic, no key needed)

```python
from finsight.extraction.kpi_extractor import KPIExtractor

extractor = KPIExtractor()

# The latest fiscal year with reported annual revenue; period="2023" (or
# "FY2023", or an exact year-end) selects another. A figure the filer did not
# report for that year is None — never taken from another year.
kpis = extractor.extract_from_companyfacts(facts, ticker="AAPL")

print(kpis.period)                          # "FY2026"
print(kpis.provenance["fiscal_year_end"])   # "2026-09-26"
print(kpis.provenance["concepts"]["revenue"])
# "RevenueFromContractWithCustomerExcludingAssessedTax"
print(kpis.provenance["missing"])           # fields not reported (here: [])
print(f"Revenue:    ${kpis.revenue:,.0f}M")
print(f"Net margin: {kpis.net_margin:.1%}")
```

### From filing text (LLM, with regex fallback) — library only

```python
# Uses GPT-4o-mini if OPENAI_API_KEY is set, otherwise a regex fallback.
# The REST API does not call this; it reads XBRL only.
# Only the first 8,000 characters are read (KPIExtractor(max_text_chars=...)), so
# pass the section that holds the figures: a whole filing's opening is its cover page.
text_kpis = extractor.extract_from_text(text, ticker="AAPL", period="2023")
# Every value is coerced to its field's type ("$10.5B" -> 10500.0; unusable -> None).
# Revenue guidance counts only when it is FULL-YEAR (text_kpis.guidance_period == "annual");
# quarterly or unlabelled guidance is ignored by the DCF, which says so in its sources.
```

Without a key, six fields are read by pattern, and only the XBRL route above is fit to
rely on:

| The text says | The field reads |
|---|---|
| `Free cash flow was negative $1.2 billion.`, `($1.2 billion)`, `a loss of $1.2 billion` | `-1200.0` |
| `Net income was -$3.0 billion.` / `Net loss of $3.0 billion.` | `-3000.0` |
| `Revenue increased $5.1 billion to $94.9 billion.` | `94900.0`, the level after "to" |
| `Revenue increased $5.1 billion.` | `None`: the sentence states only a change |
| `Net income reflects a loss of $1 billion.` | `None`: it says what the field was hit by, not what it was |
| `Net income excluding a $1 billion charge was $3 billion.` | `1000.0`: **a known misread**, the first figure wins |
| `Revenue in 2025 was $94.9 billion.` | `None`: another number stands between the label and the figure |

---

## 3. DCF valuation

### Inferred from KPIs

```python
from finsight.models.dcf import DCFModel

dcf = DCFModel().value_from_kpis(kpis)
print(dcf)                                  # DCFResult(intrinsic=$..., EV=$...B, TV%=...)
print(f"Intrinsic value: ${dcf.intrinsic_value_per_share:.2f}/share")
print(f"Enterprise value: ${dcf.enterprise_value/1e3:.1f}B")

# What the projection did to the reported figures it started from:
for name, what in dcf.assumptions.adjustments.items():
    print(name, "—", what)                  # e.g. CapEx fading from reported to the D&A rate
print(dcf.projected_capex_pct)              # CapEx / revenue used in each projection year
```

CapEx starts at the company's reported CapEx / revenue in year 1 and moves in
equal steps to its D&A rate by the final year; the terminal year stays at D&A.
Set `terminal_capex_as_pct_revenue` on explicit assumptions to choose a
different steady state (left `None`, CapEx is held flat).

### With explicit assumptions and sensitivity table

```python
from finsight.models.dcf import DCFModel, DCFAssumptions

assumptions = DCFAssumptions(
    base_revenue=394_000,                   # $394B baseline ($M)
    revenue_growth_rates=[0.08, 0.07, 0.06, 0.05, 0.05],
    terminal_growth_rate=0.03,
    ebitda_margin=0.32,
    wacc=0.09,
    net_debt=33_000,                        # debt − cash ($M)
    shares_outstanding=15_500,              # diluted shares (millions)
)

result = DCFModel(n_years=5).run(assumptions)
print(result.sensitivity_table)             # pandas DataFrame: WACC × terminal growth
```

---

## 4. Comparable company analysis

```python
from finsight.ingestion.market_data import MarketDataClient
from finsight.models.comps import CompsAnalysis

comps = CompsAnalysis(MarketDataClient())
result = comps.analyze("AAPL", peers=["MSFT", "GOOGL", "META"])

print(result)                               # CompsResult(AAPL | Implied EV: $low M – $mid M – $high M)
print(result.implied_ev_m)                  # {'low': ..., 'mid': ..., 'high': ...} implied enterprise value in $M
print(result.peer_multiples)                # pandas DataFrame of peer multiples
print(result.excluded_multiples)            # peer multiples left out of the stats (negative/zero/non-finite), with the reason
```

---

## 5. Sentiment analysis

```python
from finsight.ingestion.transcripts import TranscriptFetcher
from finsight.sentiment.analyzer import SentimentAnalyzer

analyzer = SentimentAnalyzer()              # lexicon mode (fast, no extra deps)

# Quick single-text scoring
r = analyzer.analyze_text("Revenue grew 15% driven by strong AI demand.", "transcript")
print(r.sentiment_label, r.net_sentiment)   # Bullish 0.14285714285714285 ("strong" is its one tone word, of 7)

# Full earnings-call transcripts: a third-party scrape, so it requests nothing and
# finds none unless FINSIGHT_LIVE_TRANSCRIPTS=1 (its terms are yours to check first).
fetcher = TranscriptFetcher()
transcripts = fetcher.search_transcripts("AAPL", limit=4)
for t in transcripts:
    fetcher.fetch_transcript_text(t)
    res = analyzer.analyze_transcript(t)
    print(f"{t.quarter}: {res.overall.sentiment_label} "
          f"(net={res.overall.net_sentiment:+.3f})")
```

### Deep-learning sentiment (optional)

```python
# Requires the finbert extra: pip install -e ".[finbert]"
analyzer = SentimentAnalyzer(use_finbert=True)   # FinBERT beside the word lists: see r.finbert_net,
                                                 # r.blended_tone (unvalidated 0.6/0.4), r.finbert_status
```

---

## 6. Generate a research report

```python
from finsight.report.generator import ReportGenerator

gen = ReportGenerator()
report = gen.generate(
    "AAPL",
    kpis=kpis,
    dcf_result=dcf,
    company_name="Apple Inc.",
    current_price=190.00,
    splits=[],   # MarketDataClient.get_splits; left out, the history is unknown
                 # and the report withholds model vs price
)

print(gen.to_markdown(report))              # human-readable
gen.save_markdown(report, "AAPL_report.md")
gen.save_json(report, "AAPL_report.json")   # machine-readable
```

---

## 7. REST API

Start the server (either command works):

```bash
uvicorn finsight.api.app:app --reload --port 8001
# or, via the installed console script (127.0.0.1:8001, no reload, loads .env;
# FINSIGHT_HOST / FINSIGHT_PORT / FINSIGHT_RELOAD=1 override):
finsight
```

Interactive docs: **http://localhost:8001/docs**

```bash
# Health check
curl http://localhost:8001/health

# Recent filings
curl "http://localhost:8001/filings/AAPL?form_type=10-K&limit=3"

# Extract KPIs
curl "http://localhost:8001/kpis/AAPL"

# DCF valuation (tunable assumptions)
curl "http://localhost:8001/dcf/AAPL?wacc=0.09&terminal_growth=0.03&n_years=5"

# Sentiment from recent transcripts
curl "http://localhost:8001/sentiment/AAPL?n_transcripts=4"

# Full research report (Markdown)
curl "http://localhost:8001/report/AAPL"
```

---

## 8. Web frontend (demo)

```bash
cd frontend
npm install
npm run dev
# → http://localhost:3002
```

The dashboard calls the API at `NEXT_PUBLIC_API_URL`, baked in at build time
(`http://localhost:8001` under `next dev` only; a production build without it has
no engine and makes no request). Without a reachable backend it replays a
recording of the engine for the five demo tickers, and says so in its badge
("Engine recording · bundled SEC snapshots · no live backend"); any other ticker
shows the reason and no figure. If only some calls fail, the badge reads "Partial
data" and those panels read Unavailable.

---

## Troubleshooting

- **`CIK not found for ticker`** — the symbol is not in SEC EDGAR's ticker map: verify it.
- **`No annual revenue is reported in the 10-K XBRL data`** — FinSight reads 10-K facts
  only, so a foreign filer on Form 20-F or 40-F has none. A company without the concepts
  it reads gets no DCF: a bank with no operating income is a `422` that says so.
- **`SECUserAgentError` / SEC requests rejected (403)** — set
  `SEC_EDGAR_USER_AGENT` to your own name and email (e.g.
  `"Jane Doe jane.doe@example.org"`, with your details). The documentation
  placeholder and undeliverable addresses (example.com/.net/.org, the
  .example/.invalid/.localhost/.test domains, no-reply) are rejected.
- **`404 … is not in the offline demo set`** — the API runs with
  `FINSIGHT_DEMO=1`, which serves only AAPL, MSFT, NVDA, GOOGL and TSLA and
  never makes a network request. Unset it (and set `SEC_EDGAR_USER_AGENT`) to
  analyse other companies.
- **`OPENAI_API_KEY not set` warning** — expected; KPI extraction falls back to
  regex/XBRL. Set the key to enable LLM extraction.

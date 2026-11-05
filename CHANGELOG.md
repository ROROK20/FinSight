# Changelog

## [0.1.0] — first public release (2026-11-04)

FinSight reads a company's 10-K financials from SEC EDGAR, cites every figure to its
filing, and builds a DCF and a research report from them.

### What ships

- **Engine (Python 3.11+).** `EdgarClient` (SEC filings and XBRL facts), `KPIExtractor`
  (one fiscal year's figures, each cited to its 10-K), `DCFModel` (a five-to-ten-year
  projection with a sensitivity table), `CompsAnalysis` (peer multiples from Yahoo Finance),
  `SentimentAnalyzer` (a curated finance word list, optional FinBERT) and `ReportGenerator`
  (Markdown and JSON).
- **REST API (FastAPI).** `/health`, `/meta`, `/filings`, `/kpis`, `/dcf`, `/sentiment` and
  a Markdown `/report`.
- **Dashboard (Next.js 15.5, React 19).** Prints only what the engine answers, and says why
  when it has no figure.
- **Demo mode (`FINSIGHT_DEMO=1`, or `make demo`).** No keys and no network request: real SEC
  companyfacts for AAPL, MSFT, NVDA, GOOGL and TSLA, retrieved 2026-11-04, run through the
  live code, with dated 2026-10-30 closing prices.
- **A recording for a deploy with no backend.** `frontend/lib/recordedDemo.json` holds the
  engine's own answers under a badge that says so, and a test fails if it drifts.
- **Maintainer tools.** `scripts/refresh_snapshots.py`, `scripts/record_demo.py`,
  `e2e/smoke.mjs` and `e2e/capture.mjs`.

### What a user must know

- The model value is a mechanical output of one year's figures at generic assumptions (10%
  WACC, 3% terminal growth unless you pass others). It is not a price target, a rating or
  investment advice, and it is not rolled forward to the date of the price beside it.
- Live SEC requests need `SEC_EDGAR_USER_AGENT` set to your own name and email. FinSight ships
  no fallback identity, and answers `503` without one.
- Check a response's `mode`. Without `FINSIGHT_DEMO`, a bundled ticker can fall back to its
  fixture when SEC fails, tagged `fallback_reason`.
- A company missing a concept the model needs is refused, not estimated: a bank with no
  operating income gets its `/kpis` and a `422` from `/dcf`. So is an operating loss.
- Bundled prices are a dated close, never a live quote. The demo runs no split check.

### Known limits

- **No mode of the API scores a real earnings call today.** The only transcript source is a
  third-party page, off unless `FINSIGHT_LIVE_TRANSCRIPTS=1`, which answered HTTP 404 when
  checked on 2026-11-01. Bundled sentiment is a synthetic placeholder, tagged as such.
- Tesla's demo value ($13.19) is overstated by about 3%: its debt excludes $1,569M of current
  debt that the us-gaap facts cannot prove is left out. The response says so.
- Debt reads a list of common tags, not every tag, so a filer using others can read low.
  Marketable securities, minority interests and preferred stock are not netted, finance leases
  are not in debt or CapEx, and a 10-K/A is not read.
- Share counts and EPS are as each filing reported them, not adjusted for a later split.
- D&A is depreciation only for a filer reporting neither a total nor amortization (GOOGL and
  TSLA in the demo), which understates EBITDA and year-1 FCF.
- Peer groups are a hand-picked list for nine tickers. The word-list classifier is a heuristic
  over sentence patterns, not a parser, and FinBERT's blend weights have not been validated.
- Deployed publicly, the API has no authentication or rate limiting, and CORS is `*`. See
  [SECURITY.md](SECURITY.md).

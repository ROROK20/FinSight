# FinSight Architecture

FinSight is a layered Python package for automated equity research, plus a
Next.js demo frontend. Each layer has a single responsibility and a clean
interface to the next, so components can be used independently or composed into
a full pipeline.

---

## High-level data flow

```
                          ┌─────────────────────────────┐
   Ticker symbol  ───────▶│        Ingestion layer       │
                          │  finsight/ingestion/         │
                          │   • edgar.py    (SEC EDGAR)  │
                          │   • transcripts.py           │
                          │   • market_data.py           │
                          └──────────────┬───────────────┘
                                         │ filings, XBRL facts,
                                         │ transcripts, market profiles
                                         ▼
                          ┌─────────────────────────────┐
                          │       Extraction layer       │
                          │  finsight/extraction/        │
                          │   • kpi_extractor.py         │
                          │   • financial_parser.py      │
                          └──────────────┬───────────────┘
                                         │ ExtractedKPIs
                  ┌──────────────────────┼──────────────────────┐
                  ▼                      ▼                      ▼
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │   Models layer   │  │   Models layer   │  │  Sentiment layer │
        │  models/dcf.py   │  │  models/comps.py │  │ sentiment/       │
        │  → DCFResult     │  │  → CompsResult   │  │  analyzer.py     │
        └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
                 │                     │                     │
                 └──────────────┬──────┴──────┬──────────────┘
                                ▼             ▼
                          ┌─────────────────────────────┐
                          │        Report layer          │
                          │  finsight/report/generator   │
                          │   → EquityReport (MD + JSON) │
                          └──────────────┬───────────────┘
                                         │
                          ┌──────────────┴───────────────┐
                          ▼                              ▼
                ┌──────────────────┐          ┌──────────────────┐
                │   REST API       │  HTTP    │   Web frontend   │
                │ finsight/api/app │◀────────▶│  frontend/ (Next)│
                └────────┬─────────┘          └──────────────────┘
                         │ FINSIGHT_DEMO=1
                         ▼
                ┌──────────────────────────────┐
                │  finsight/api/demo_data.py   │
                │  bundled SEC snapshots → the │
                │  SAME code, zero network     │
                └──────────────────────────────┘
```

**Demo mode is a short-circuit at the API layer, not a separate pipeline.** With
`FINSIGHT_DEMO=1` the network is skipped and bundled SEC companyfacts snapshots
(`finsight/data/companyfacts/`, retrieved 2026-11-04) are fed through the same
extraction / DCF / sentiment / report code the live path runs, so demo
responses have the same shape, the same computation and the same provenance as
live ones. (Sentiment is the exception to "bundled SEC data": it scores
earnings-call text, never filings, and demo mode bundles no transcripts.)
See [Demo mode](#demo-mode--finsightapidemo_datapy) below.

---

## Components

### Ingestion — `finsight/ingestion/`

Responsible for pulling raw data from external sources. No analysis happens here.

| Module           | Key class           | Responsibility |
| ---------------- | ------------------- | -------------- |
| `edgar.py`       | `EdgarClient`       | Resolve ticker → CIK, list filings (10-K/10-Q/8-K), download filing text, and fetch XBRL `companyfacts`. Sends the caller's own `User-Agent` (`SEC_EDGAR_USER_AGENT`, required — no fallback identity; undeliverable contacts are refused), spaces requests with one process-wide thread-safe limiter (≤ ~8.3 req/s), caches the ticker map (24 h) and companyfacts (10 min). |
| `transcripts.py` | `TranscriptFetcher` | Scrape earnings-call transcripts from Motley Fool pages, only with `FINSIGHT_LIVE_TRANSCRIPTS=1` (off, it requests nothing and the API says "not available"). The listing answered HTTP 404 on 2026-11-01, so runs with it on currently fall back to the labelled placeholder; a miss is remembered in memory for an hour. Accepts only links naming the requested ticker and only `https` URLs on fool.com (`is_fool_url`; off-host links are never requested), spaces every request on one shared 1 req/s limiter, caches real transcripts for 24 h under a sanitised file name. |
| `market_data.py` | `MarketDataClient`  | Fetch price/market profiles; holds the hand-picked peer lists comps uses (`CURATED_PEERS`). |

The SEC EDGAR APIs are **free and key-less**. The only configuration the SEC
requires is a `User-Agent` naming the requester, read from `SEC_EDGAR_USER_AGENT`;
without it a live request raises `SECUserAgentError` instead of borrowing an identity.

### Extraction — `finsight/extraction/`

Turns raw filings into structured metrics.

- `kpi_extractor.py` → **`KPIExtractor`** produces an **`ExtractedKPIs`**
  dataclass. Two paths:
  - `extract_from_companyfacts(facts, ticker, period=None)` — the API's and
    demo mode's single loader. It reads every concept in
    `KPIExtractor.XBRL_CONCEPTS` in its own unit (`USD`, `shares`,
    `USD/shares`), keeps only fiscal-year facts, anchors ONE fiscal year on
    annual revenue (the latest by default, or `period="2023"` / `"FY2023"` /
    an exact year-end), and reads every other figure at exactly that
    year-end. A concept with no value there stays `None` and is listed in
    `provenance["missing"]` — it is never filled from another year. Concept
    chains cover filers that tag differently (e.g. `Revenues` vs the ASC 606
    revenue concept; `PaymentsToAcquireProductiveAssets` for capex), and
    EBITDA (operating income + D&A; PP&E `depreciation` and acquired-intangible
    `amortization_of_intangibles` are also read separately, and the DCF's
    steady-state CapEx is D&A less that amortization where reported, else the
    combined D&A rate — disclosed in `limitations`. D&A is the filer's total
    D&A concept, else
    `Depreciation` + `AmortizationOfIntangibleAssets`, else `Depreciation`
    alone, flagged in `provenance["limitations"]` as understated), FCF
    (CFO − capex; finance leases are in neither, also flagged), total debt (current
    debt added only where `LongTermDebt` is shown to exclude it; flagged in
    `limitations.current_debt` where that is unsettled; a filer with neither
    `LongTermDebt` nor `LongTermDebtNoncurrent` is read from a list of common wider
    tags, a total concept winning over its components, and can read low under any other
    tag; `/dcf` names what it used in `total_debt_concepts`) and, where
    `GrossProfit` is not tagged, gross profit are derived. `provenance` names
    the fiscal year, year-end, the year's own 10-K (earliest filing that
    reported it), the concept or formula behind every field, the 10-K each
    concept's value was read from (`figure_filings`) and the per-share basis
    (as reported, not split-adjusted). `extract_from_xbrl(series, …)` is the same logic over
    pre-extracted series.
  - `extract_from_text(...)` — a library function, not wired to any API
    endpoint: an LLM (GPT-4o-mini) extracts KPIs from unstructured text when
    `OPENAI_API_KEY` is set, with a regex fallback when it is not.
- `financial_parser.py` → **`FinancialParser`** + **`FinancialHistory`**, the
  statement-normalization helpers: `normalize_xbrl_to_df` turns an XBRL concept
  series into a tidy `$M` DataFrame indexed by period end, and
  `compute_growth_rates` / `compute_margins` / `ttm` derive series-level figures
  from it. `KPIExtractor` uses the first two for `revenue_growth_yoy`, comparing
  the extracted year with the fiscal year immediately before it, under the same
  revenue concept where the filer reports one.
  `parse_yfinance` builds a `FinancialHistory` from yfinance statement frames.

### Models — `finsight/models/`

Valuation engines that consume `ExtractedKPIs`.

- `dcf.py` → **`DCFModel`** + **`DCFAssumptions`** → **`DCFResult`**. Projects
  5–10 years of free cash flow, discounts at WACC, adds a Gordon-growth terminal
  value, and emits a WACC × terminal-growth **sensitivity table** (`pandas`).
  `value_from_kpis()` infers assumptions from one fiscal year's reported
  figures — revenue growth and CapEx both start at the reported rate in year
  1 and move linearly to a steady state by year *n* (terminal growth; the D&A
  rate) — and records each input's source in `sources` and every departure
  from a reported figure in `adjustments`. Net debt is total debt − cash and
  cash equivalents (marketable securities, current and non-current, not
  netted; noncontrolling interests and preferred stock not deducted); shares
  are the diluted
  weighted-average count (period-end basic, labelled, only when no diluted
  count is reported; a year-end count above it in the same 10-K is disclosed in
  `limitations.share_count`, with what it would move, not used). `run()` accepts fully
  explicit assumptions. Its projected
  FCF is EBIT × (1 − tax) + D&A − CapEx − ΔNWC, not the reported figure: it starts
  from operating income, so stock-based compensation stays a cost and is never added
  back, at a flat 21% tax and with working capital at 2% of the revenue change.
  Reported FCF (`kpi_extractor.py`) is operating cash flow − CapEx, where stock comp
  is added back as non-cash and taxes paid, interest and working capital are as they
  happened. `limitations.free_cash_flow_basis` gives both for the ticker; no stock-comp
  concept is read, so the gap is not split.
- `comps.py` → **`CompsAnalysis`** → **`CompsResult`**. Benchmarks the target
  against a peer group across `ev_revenue`, `ev_ebitda`, `pe`, `ps`, `pb`,
  `p_fcf`, and produces implied valuation ranges (low / mid / high). Two
  properties of the output matter when reading it:
  - **EV and equity results are separate.** `implied_ev_m` aggregates only the
    enterprise-value multiples (`ev_revenue`, `ev_ebitda`); `implied_equity_m`
    only the price multiples (`pe`, `ps`). Mixing them would add an enterprise
    value to an equity value. Neither is a per-share figure — converting needs
    net debt and a share count, which comps does not carry.
  - **No enterprise value for a financial company.** A target in Yahoo's "Financial
    Services" sector (or, with no sector of its own, one most of whose peers are) has its EV
    multiples skipped, with the reason in `ev_refused` and `skipped_multiples`.
  - **No invented peers.** A symbol whose profile cannot be fetched, or holds
    no multiple, is left out and listed in `CompsResult.unavailable` with the reason; with no peer
    data the report's comps section says "Unavailable" and shows no figure.
    No multiple is ever invented.
  - **One EBITDA basis.** When the target's 10-K EBITDA is
    depreciation-only (`provenance.limitations.depreciation_amortization`),
    peers' Yahoo EV/EBITDA (full D&A) is not applied to it and
    `CompsResult.skipped_multiples` says why; the report marks the row "Not
    applied". Peers' multiples are trailing-twelve-month, the target's figures
    its fiscal year — the report says the periods differ.
  - **Loss-making peers do not drag the aggregate.** A negative,
    zero or non-finite peer multiple (a negative EV/EBITDA or P/E is a loss,
    not a price) is left out of that multiple's peer statistics and listed in
    `CompsResult.excluded_multiples` with the reason; the report prints the
    list. The raw figure stays in `peer_multiples`, and no implied value is
    ever computed from a non-positive multiple.

### Sentiment — `finsight/sentiment/`

- `analyzer.py` → **`SentimentAnalyzer`** → **`SentimentResult`** /
  **`TranscriptSentiment`**. Default mode counts words against FinSight's own
  curated finance word lists (`POSITIVE`, `NEGATIVE`, `UNCERTAINTY`: 161
  hand-picked entries inspired by, but not, the Loughran-McDonald dictionary;
  only `stem*` entries match by prefix) — fast and dependency-light. With
  `use_finbert=True` it also reports FinBERT (`finbert_net`, `finbert_status`)
  and a `blended_tone` on one [−1, 1] scale (fixed 0.6 / 0.4, never validated);
  `net_sentiment` stays the word-list score. Guidance
  tone counts only an explicit action on guidance / outlook / forecast
  (`GUIDANCE_TONES`: raised, lowered, mixed, maintained, initiated, withdrawn,
  else `not_stated`): the verb must govern the guidance phrase directly
  ("raised our full-year guidance") in a declarative, realised clause — past,
  present or present-progressive, or "we now expect … above prior guidance";
  negations, modals, conditionals, plans and questions ("not raising", "may
  raise", "if demand holds", "too early to raise", "…raise guidance?") do not
  count, nor does an action on another topic ("guidance on capital
  expenditures", "our forecast for credit losses", "our outlook on inflation"), by
  another party ("raised by peers", "Microsoft raised its forecast", "S&P lowered our
  outlook": the speaker must be the verb's own subject) or dated to a past period ("last quarter we lowered", "raised our
  guidance twice last year"),
  and results measured against guidance ("came in above our guidance") are
  not a change. With `FINSIGHT_LIVE_TRANSCRIPTS` off, live `/sentiment` and `/report`
  request nothing and say "not available". On, when no real transcript can be fetched,
  the API analyses `TranscriptFetcher.create_mock_transcript` — a boilerplate
  placeholder with `synthetic=True` — and every response and report built from
  it says so.

### Report — `finsight/report/`

- `generator.py` → **`ReportGenerator`** assembles KPIs, DCF, comps, and
  sentiment into an **`EquityReport`**: the DCF model value beside the price
  and the gap between them (`model_value_per_share`, `model_vs_price`), a
  factual summary and highlights, and a Data Sources section listing the
  filing, the price source, every DCF default and adjustment. It deliberately
  carries **no rating or price target** — the model value is a mechanical
  output of one fiscal year's figures at stated assumptions (by default a
  generic 10% WACC), and the report says so above the fold. Sentiment from the
  synthetic placeholder transcript is never quoted in the summary. Serializes
  to Markdown (`to_markdown`) and JSON (`to_dict` / `save_json`).

### API — `finsight/api/`

- `app.py` exposes the pipeline over HTTP with **FastAPI**. Heavy clients are
  constructed once as module-level singletons. Interactive docs are served at
  `/docs`. The installed `finsight` console script (and `main()`) launch the
  server on 127.0.0.1 without reload, after loading `.env`. `/kpis`, `/dcf` and
  `/report` share one loader (`KPIExtractor.extract_from_companyfacts`), so all
  three describe the same fiscal year, and each JSON response carries `mode`,
  the fiscal year and `provenance`. CORS is open (`*`) — the API is public, read-only and credential-free.

| Endpoint | Returns | Demo mode (`FINSIGHT_DEMO=1`) |
| -------- | ------- | ----------------------------- |
| `GET /health` | Liveness, plus `demo_mode` and the bundled ticker list | n/a — always live |
| any ticker endpoint, ticker outside the bundled five | — | 404 with a `detail` saying demo mode is offline; no request is made |
| `GET /meta/{ticker}` | Company name, sector, current price | Bundled name/sector and the dated 2026-10-30 close (`price_date`, `price_source`) |
| `GET /filings/{ticker}` | Recent SEC filing index | Declines: empty list + reason (see below) |
| `GET /kpis/{ticker}` | `ExtractedKPIs` for one fiscal year (latest by default, `?period=`), with `provenance` | Live extraction over the bundled SEC snapshot |
| `GET /dcf/{ticker}` | DCF valuation (`wacc`, `terminal_growth`, `n_years` and `period` params — `period` picks the base fiscal year as `/kpis` does; omitted ones are reported as defaults) with `fiscal_year`, `valuation_date` (the base fiscal year-end the value is struck at, not the price date), `provenance`, `assumption_sources`, `defaulted_inputs`, `adjustments` and the `capex_pct_schedule`; 422 on missing revenue/shares or a WACC − g spread under 2 pp | Real DCFModel over the snapshot's latest year |
| `GET /sentiment/{ticker}` | Per-quarter tone, guidance, risk mentions; each result's `synthetic` flag and `source` | Real analyzer over the synthetic placeholder transcript, tagged `synthetic: true` |
| `GET /report/{ticker}` | Full research report (Markdown, `text/plain`) with a Data Sources section | Real generator over the bundled data, comps omitted |

### Demo mode — `finsight/api/demo_data.py`

`FINSIGHT_DEMO=1` makes every ticker endpoint answer for **AAPL, MSFT, NVDA,
GOOGL, TSLA** with **no network access at all** — no SEC EDGAR, no Yahoo
Finance, no transcript scrape, no API key — from three bundled inputs:

- **Financials:** real SEC companyfacts per ticker (retrieved 2026-11-04 by
  `scripts/refresh_snapshots.py`, trimmed to 10-K facts for the concepts FinSight reads that
  day, for periods ending after a per-file date stated in `_snapshot.contents`,
  so a listed concept absent from a file has no fact *in its window*;
  `_snapshot.not_carried` is empty, so `finsight.data.KNOWN_SNAPSHOT_GAPS` is too;
  values unmodified, bytes pinned by `companyfacts/SHA256SUMS`), read by
  `extract_from_companyfacts` exactly as the live `/kpis` reads a fresh
  payload. Each ticker's latest fiscal year is the one served (AAPL FY2026,
  MSFT FY2026, NVDA FY2026, GOOGL FY2025, TSLA FY2025), and every response
  cites its fiscal year-end and 10-K accession. `/kpis?period=` can select an
  earlier year in the snapshot. The earliest year has no revenue growth because
  the snapshot was trimmed before its prior year, and the response says so.
- **Prices:** the Yahoo Finance close on 2026-10-30, dated in `/meta`
  (`price_date`) and the report.
- **Sentiment:** the synthetic placeholder transcript, tagged
  `synthetic: true` — demo mode bundles no transcripts.

Every JSON response carries `"mode": "demo"`; `/report` is plain text, so it
carries a Markdown banner instead. Two endpoints answer by declining rather than
inventing data:

- **`/filings`** returns an empty index with a `detail` explaining why. Accession
  numbers, filing dates and archive URLs are verifiable public records —
  shipping invented ones would be fabricated data wearing the costume of a real
  SEC record.
- **`/report`** omits the comparable-company section, because peer multiples
  need live market data and the only offline alternative would be synthetic
  peers presented as real.

**Scope of the guarantee:** every ticker. The five bundled tickers are served
offline; any other ticker is refused with a 404 whose `detail` says demo mode is
offline and how to run live (`demo_data.not_bundled_detail`). It is never looked
up live. Without the flag, the live path runs first and the bundled data acts only
as a fallback when a network call fails (the response then carries
`fallback_reason`) — which is why a response's `mode` field, not the
environment, is the thing to check.

### Frontend — `frontend/`

A **Next.js 15.5** app on **React 19** (TypeScript + Tailwind),
running on port **3002**. It is wired to the real API: `lib/api.ts` calls
`/meta`, `/kpis`, `/dcf` and `/sentiment` on `NEXT_PUBLIC_API_URL`, baked in at
build time (`http://localhost:8001` under `next dev` only; a production build
without it has no engine and makes no request), and `lib/research.ts::mergeEngineData`
turns those responses into the page. There is no in-browser baseline: a figure is
the engine's or it is absent. With no engine at all, the same merge runs over
`lib/recordedDemo.json`, the engine's own responses for the five demo tickers as
`scripts/record_demo.py` recorded them; `tests/test_recorded_demo.py` regenerates
it and fails on any drift. A header badge, derived from all four calls, reports
**Live data** (every call answered `mode: live`), **Demo data** (at least one part
is bundled demo data), **Partial data** (some calls failed; their panels read
Unavailable), **Engine recording · bundled SEC snapshots · no live backend** (no
call answered, ticker recorded) or **Offline · no data** (no call answered, ticker
not recorded: the reason and no figure) — so the page never implies more than it
has. A response without `mode` counts as failed. A `/dcf` that refuses the ticker with a reason
(422, or 404 from the engine) is an answer rather than a failure: the page shows
the reason and no number. The sentiment panel is tagged Synthetic when
the engine computed it from the placeholder transcript; the DCF panel states
its fiscal-year basis, how many inputs were defaults and how many reported
figures the projection adjusted (each listed in a numbered note).

A peer table, multi-year history, catalysts and risks have no endpoint behind
them, so the page names them as absent rather than invent them. Its two charts
plot the DCF's projected revenue and modelled FCF, from the `/dcf` response; the
*Reported FCF* row is the other figure, from `/kpis` (see `dcf.py` above).

---

## Design principles

- **Layered & composable.** Every layer returns plain dataclasses, so any stage
  can be used on its own or wired into the full pipeline.
- **Graceful degradation.** Optional dependencies (LLM, FinBERT) enhance results
  but are never required — there is always a deterministic fallback.
- **Key-less by default.** The core flow (XBRL → DCF/comps → report) needs no
  API keys; only live SEC requests need a `SEC_EDGAR_USER_AGENT`.
- **Say which data you are showing.** Every demo response is tagged, a peer with no
  data is left out and named in the result, and what has no endpoint behind it is named
  as absent. A figure that is not real should never look real — this is
  a correctness property here, not a presentation choice.
- **Deterministic, offline tests.** The `pytest` suite runs without network
  access for fast, reliable CI, and the demo path's no-network guarantee is
  pinned by tests that make every network method raise.

---

## Configuration

| Variable               | Required | Purpose |
| ---------------------- | -------- | ------- |
| `FINSIGHT_DEMO`        | Optional | `1` = serve the bundled SEC snapshots offline for the five demo tickers and refuse every other ticker (no network, no keys). Unset = live path with the snapshots as a fallback. |
| `SEC_EDGAR_USER_AGENT` | For live SEC requests | Your own name and a contact email that reaches you (format: `"Jane Doe jane.doe@example.org"` — with your details; that example address is itself rejected), sent as the User-Agent the SEC requires. No fallback anywhere, including `docker-compose.yml`, which passes through only what you set. Unset, the placeholder, a value without an email, or only undeliverable addresses (example.com/.net/.org, the .example/.invalid/.localhost/.test domains, no-reply) make live SEC requests fail with a 503 naming this variable. Not read in demo mode. |
| `NEXT_PUBLIC_API_URL`  | Frontend | Where the dashboard looks for the API, baked in at build time (`http://localhost:8001` under `next dev` only; a production build without it has no engine). |
| `OPENAI_API_KEY`       | Optional | Enables LLM KPI extraction; regex/XBRL fallback otherwise. |
| `OPENAI_MODEL`         | Optional | Override the LLM model (default `gpt-4o-mini`). |
| `OPENAI_BASE_URL`      | Optional | Point at an OpenAI-compatible endpoint. |
| `FINSIGHT_HOST` / `FINSIGHT_PORT` | Optional | Override the `finsight` console script's bind (default `127.0.0.1:8001`; the Docker image binds `0.0.0.0` itself). |
| `FINSIGHT_RELOAD` | Optional | `1` = auto-reload for development (off by default). |

See [`.env.example`](../.env.example) for a copy-paste template.

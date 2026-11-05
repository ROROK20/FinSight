# FinSight Frontend

Interactive equity research dashboard built with Next.js 15.5 + React 19 +
Tailwind + Recharts.

## Quick start

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3002

The dashboard calls the real FinSight API at `NEXT_PUBLIC_API_URL`, which is
baked in at build time. Under `next dev` it defaults to `http://localhost:8001`.
A production build without it has no engine and makes no request: it replays
**recorded engine output** (below). So does a configured backend that does not
answer, so `npm run build` works with no backend running. `npm test` runs the
merge, DCF-refusal, format and URL-resolution unit tests.

## Features

Driven by the API (`/meta`, `/kpis`, `/dcf`, `/sentiment`):

- Ticker-driven research card: price, DCF model value and the gap between them (a mechanical model output — no rating or price target)
- 7 key financial metrics
- DCF valuation panel, and two charts of the DCF's own projected revenue and free
  cash flow (model output from `/dcf`, not a forecast or reported history)
- Earnings sentiment + guidance tone, scored on earnings-call text, never on
  filings. No mode scores a real call today: the only transcript source is off
  unless the server sets `FINSIGHT_LIVE_TRANSCRIPTS=1` (the panel then says "not
  available") and was down when last checked, so the demo and the recording show
  the placeholder's score, tagged **Synthetic**

Not on the dashboard: a peer table, multi-year history, catalysts and risks. The
API serves no endpoint for them here, and the page names them as absent rather
than invent them.

5 pre-loaded tickers (AAPL, MSFT, NVDA, GOOGL, TSLA) have bundled backend
data (real SEC filings, dated prices). Against a live engine, a company that files
10-Ks with the concepts it reads works: a Form 20-F filer gets no figures, and a
company with no operating income, such as a bank, gets no DCF.
Against an offline demo engine (`FINSIGHT_DEMO=1`, as `make demo` runs it) any
other ticker is **not looked up**: the dashboard reads `/health`, shows a
**Not in demo set** badge and a banner naming the covered tickers and how to
run live, and shows no figure.

## With no backend: a recording

A deploy with no engine shows `lib/recordedDemo.json`: the engine's own `/meta`,
`/kpis`, `/dcf` and `/sentiment` responses for those five tickers, run in-process
with `FINSIGHT_DEMO=1` over the bundled SEC snapshots and merged exactly as a
running engine's answers are. The badge reads **Engine recording · bundled SEC
snapshots · no live backend**, a banner gives the capture date, and the sentiment
panel stays tagged **Synthetic**. Any other ticker is an empty state with the
reason and no figure.

```bash
python scripts/record_demo.py           # retake it (from the repo root)
python scripts/record_demo.py --check   # exit 1 if it has drifted from the engine
```

`tests/test_recorded_demo.py` regenerates the recording and fails on any drift,
so an engine change cannot leave a stale figure in a deploy.

## Data-mode badge

The header badge is derived from all four engine calls (`/meta`, `/kpis`,
`/dcf`, `/sentiment`): **Live data** only when every one answered `mode: live`;
**Demo data** when the engine answered but at least one part is bundled demo
data; **Partial data** when some calls failed — those panels read **Unavailable**
and nothing replaces them; **Not in demo set** when a demo engine does not bundle
the ticker (nothing is fetched, no figure shows); **Engine recording · bundled SEC
snapshots · no live backend** when none answered and the recording holds the
ticker; **Offline · no data** when none answered and it does not. A response
without a `mode` is treated as a failed call. A `/dcf` that refuses the ticker
with a reason (422, or 404 from the engine) is an answer, not a failure: the page
shows that reason and no number. The sentiment panel is tagged **Synthetic** whenever the engine computed it from
the placeholder transcript, and the DCF panel states its basis (fiscal year and
10-K) and how many inputs were model defaults. While loading, the badge reads
"Loading engine…" and no panel claims a source. The copy for every state lives
in one typed `Record<DataMode, …>` map, so a state without copy is a compile
error.

## Build for production

```bash
npm run build
npm start
```

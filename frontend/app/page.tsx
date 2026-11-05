"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  emptyResearch, limitationRows, linkParts, mergeEngineData, recordedResearch,
  type CompanyResearch, type Figure, type GuidanceTone, type Recording,
} from "@/lib/research";
import recordedDemo from "@/lib/recordedDemo.json";
import {
  api, tagged, demoScope, engineProblem, noDataReason, API_URL, type HealthResponse,
} from "@/lib/api";
import { fmtMoney as money, fmtPct as pct, fmtUsd as usd } from "@/lib/format";
import {
  Search, FileText, Github,
  AlertTriangle, Sparkles, BarChart3, DollarSign, Wifi, WifiOff, Loader2,
} from "lucide-react";
import {
  BarChart, Bar, ResponsiveContainer, XAxis, YAxis, CartesianGrid, Tooltip,
} from "recharts";
import clsx from "clsx";

/** The engine's answers, recorded (scripts/record_demo.py): what a no-backend deploy replays. */
const RECORDING = recordedDemo as unknown as Recording;
const RECORDED_TICKERS = Object.keys(RECORDING.tickers);

/**
 * "recorded": no engine answered, and the page replays the recording of one.
 * "empty": no engine answered and the recording does not hold this ticker.
 * "uncovered": the engine is up in offline demo mode and the ticker is not one
 * of its bundled ones, so nothing is fetched. Neither of the last two shows a
 * figure: the page says why instead.
 */
type DataMode = "live" | "demo" | "partial" | "recorded" | "empty" | "uncovered" | "loading";

/** The four engine calls behind the page, and what each one delivered. */
type Part = "meta" | "kpis" | "dcf" | "sentiment";
/** "refused": the engine answered /dcf and declined, stating why (no figure). */
type PartState = "live" | "demo" | "recorded" | "missing" | "refused";
type Parts = Record<Part, PartState>;

const partState = (r: { mode: "live" | "demo" } | null): PartState => (r ? r.mode : "missing");

/**
 * The page-level claim, derived from ALL four parts. "Live data" requires every
 * part to be live: a failed call leaves that panel Unavailable, and a page with
 * no DCF must not wear the live badge just because /meta answered.
 */
function overallMode(parts: Parts): DataMode {
  const states = Object.values(parts);
  if (states.some((s) => s === "missing")) return "partial";
  if (states.some((s) => s === "demo")) return "demo";
  return "live";
}

/**
 * Every user-visible claim about where the data came from, keyed by mode.
 *
 * This is a `Record<DataMode, …>` on purpose: the header badge and the footer
 * both state the data mode, and they must never disagree. Adding a mode without copy for it
 * is a type error.
 */
const MODE_COPY: Record<
  DataMode,
  { label: string; cls: string; Icon: typeof Wifi; title: string; footer: string }
> = {
  live: {
    label: "Live data",
    cls: "bg-accent-green/15 text-accent-green border-accent-green/40",
    Icon: Wifi,
    title:
      "Headline financials and the DCF come from the live FinSight engine (SEC EDGAR 10-K data). A panel tagged Synthetic was computed from a placeholder transcript.",
    footer: "Live data from the FinSight engine · a panel tagged Synthetic used a placeholder transcript",
  },
  demo: {
    label: "Demo data",
    cls: "bg-accent-blue/15 text-accent-blue border-accent-blue/40",
    Icon: Wifi,
    title:
      "Backend reachable, serving bundled SEC filings and dated prices through the real engine. The panel tagged Synthetic was computed from a placeholder transcript.",
    footer: "Demo: bundled SEC filings via the real engine · the panel tagged Synthetic used a placeholder transcript",
  },
  partial: {
    label: "Partial data",
    cls: "bg-orange-500/15 text-orange-400 border-orange-500/40",
    Icon: AlertTriangle,
    title: "Some engine calls failed. Their panels read Unavailable; the rest come from the engine.",
    footer: "Partial — panels reading Unavailable had a failed engine call; nothing replaces them",
  },
  recorded: {
    label: "Engine recording · bundled SEC snapshots · no live backend",
    cls: "bg-accent-blue/15 text-accent-blue border-accent-blue/40",
    Icon: WifiOff,
    title: `A recording of the FinSight engine running on its bundled SEC snapshots (retrieved ${RECORDING.snapshot_retrieved}), captured ${RECORDING.captured}. No live backend answered, so nothing was fetched.`,
    footer: `Recording of the FinSight engine on bundled SEC snapshots, captured ${RECORDING.captured} · no live backend`,
  },
  empty: {
    label: "Offline · no data",
    cls: "bg-yellow-500/15 text-yellow-400 border-yellow-500/40",
    Icon: WifiOff,
    title: "No live backend answered and the recording does not hold this ticker, so no figure is shown.",
    footer: "Offline — no live backend, and this ticker is not in the recording, so no figure is shown",
  },
  uncovered: {
    label: "Not in demo set",
    cls: "bg-orange-500/15 text-orange-400 border-orange-500/40",
    Icon: AlertTriangle,
    title:
      "The engine is in offline demo mode, which covers only its bundled tickers and makes no network requests. This ticker was not looked up, so no figure is shown.",
    footer: "Demo engine — this ticker is not bundled, so nothing was looked up and no figure is shown",
  },
  loading: {
    label: "Loading engine…",
    cls: "bg-panel text-gray-400 border-border",
    Icon: Loader2,
    title: "Contacting backend",
    footer: "Contacting the FinSight engine…",
  },
};

/** Who a live engine can answer for. */
const LIVE_SCOPE =
  "It reads 10-K XBRL facts only: a foreign filer on Form 20-F has none, and a company without the concepts it reads, such as a bank with no operating income, gets no DCF.";

/** How each guidance-tone class reads. A total map: a new class without copy is a type error. */
const GUIDANCE_COPY: Record<GuidanceTone, string> = {
  raised: "Raised",
  lowered: "Lowered",
  mixed: "Mixed (raised and lowered)",
  maintained: "Reaffirmed",
  initiated: "Initiated",
  withdrawn: "Withdrawn",
  not_stated: "Not stated",
};

/** An engine that answered with an error is there: "no live backend" would be false. */
function modeCopy(mode: DataMode, engineSaid: string | null) {
  const copy = MODE_COPY[mode];
  if (!engineSaid) return copy;
  if (mode === "recorded") {
    return {
      ...copy,
      label: "Engine recording · bundled SEC snapshots · engine could not serve this",
      title: `A recording of the FinSight engine running on its bundled SEC snapshots (retrieved ${RECORDING.snapshot_retrieved}), captured ${RECORDING.captured}. The engine answered with an error, stated on the page.`,
      footer: `Recording of the FinSight engine on bundled SEC snapshots, captured ${RECORDING.captured} · the engine could not serve this`,
    };
  }
  if (mode === "empty") {
    return {
      ...copy,
      label: "Engine error · no data",
      title: "The engine answered with an error, stated on the page, and the recording does not hold this ticker, so no figure is shown.",
      footer: "The engine could not serve this, and this ticker is not in the recording, so no figure is shown",
    };
  }
  return copy;
}

export default function Home() {
  const [query, setQuery] = useState("AAPL");
  const [selected, setSelected] = useState("AAPL");
  const [data, setData] = useState<CompanyResearch>(() => emptyResearch("AAPL"));
  const [mode, setMode] = useState<DataMode>("loading");
  // null while loading: no per-panel claim is made until the calls settle.
  const [parts, setParts] = useState<Parts | null>(null);
  // What the engine says about itself (/health); kept once it has answered.
  const [engine, setEngine] = useState<HealthResponse | null>(null);
  // What the engine said when it answered every data call with an error.
  const [engineSaid, setEngineSaid] = useState<string | null>(null);
  const engineRef = useRef<HealthResponse | null>(null);
  // Which load() is current. A response for a ticker the user has already
  // left is dropped: without this, a slow MSFT reply landing after AAPL's
  // replaced AAPL's figures (quick ticker switches showed stale results).
  const loadSeq = useRef(0);

  const load = useCallback(async (ticker: string) => {
    const seq = ++loadSeq.current;
    const stale = () => seq !== loadSeq.current;
    setMode("loading");
    setParts(null);
    setEngineSaid(null);
    setData(emptyResearch(ticker));
    const allMissing: Parts = { meta: "missing", kpis: "missing", dcf: "missing", sentiment: "missing" };
    if (!engineRef.current) {
      const health = await api.health();
      if (stale()) return;
      engineRef.current = health;
      setEngine(health);
    }
    const scope = demoScope(engineRef.current);
    if (scope && !scope.includes(ticker)) {
      // The demo engine refuses tickers it does not bundle (it never looks
      // them up live), so asking would only collect four 404s.
      setParts(allMissing);
      setMode("uncovered");
      return;
    }
    // No engine call delivered: replay the recording if it holds this ticker,
    // else say there is nothing to show. Never an invented figure.
    const noEngine = () => {
      const recorded = recordedResearch(RECORDING, ticker);
      setData(recorded ?? emptyResearch(ticker));
      setParts(recorded ? { meta: "recorded", kpis: "recorded", dcf: "recorded", sentiment: "recorded" } : allMissing);
      setMode(recorded ? "recorded" : "empty");
    };
    try {
      // Fire all engine calls in parallel; each resolves to null on failure,
      // and an untagged response is treated as a failure (see `tagged`).
      let said: string | null = null;
      const heard = async (res: Response) => {
        said ??= await engineProblem(res);
      };
      const [meta, kpis, dcfOutcome, sentiment] = await Promise.all([
        api.meta(ticker, heard).then(tagged),
        api.kpis(ticker, heard).then(tagged),
        api.dcf(ticker, heard),
        api.sentiment(ticker, heard).then(tagged),
      ]);
      if (stale()) return;
      const dcf = tagged(dcfOutcome.dcf);
      const dcfRefusal = dcf ? null : dcfOutcome.refusal;
      if (!meta && !kpis && !dcf && !sentiment && !dcfRefusal) {
        setEngineSaid(said);
        noEngine();
        return;
      }
      const next: Parts = {
        meta: partState(meta),
        kpis: partState(kpis),
        dcf: dcfRefusal ? "refused" : partState(dcf),
        sentiment: partState(sentiment),
      };
      setData(mergeEngineData(ticker, { meta, kpis, dcf, sentiment, dcfRefusal }));
      setParts(next);
      setMode(overallMode(next));
    } catch {
      if (stale()) return;
      noEngine();
    }
  }, []);

  /** A panel whose engine call failed: it reads Unavailable, and nothing replaces it. */
  const missing = (part: Part) => parts !== null && parts[part] === "missing";

  useEffect(() => {
    load(selected);
  }, [selected, load]);

  const ModeBadge = () => {
    const cfg = modeCopy(mode, engineSaid);
    const { Icon } = cfg;
    return (
      <span
        title={cfg.title}
        className={clsx(
          "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold border",
          cfg.cls,
        )}
      >
        <Icon className={clsx("w-3.5 h-3.5", mode === "loading" && "animate-spin")} />
        {cfg.label}
      </span>
    );
  };

  /** Marks a panel whose engine call failed. No figure stands in for it. */
  const UnavailableTag = ({ what }: { what: string }) => (
    <span
      title={`${what}: the engine call behind it failed or returned an unverifiable response, so nothing is shown.`}
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-orange-500/10 text-orange-300 border border-orange-500/30"
    >
      <AlertTriangle className="w-2.5 h-2.5" aria-hidden="true" />
      Unavailable
    </span>
  );

  /** Marks the sentiment panel when it was computed from the placeholder transcript. */
  const SyntheticTag = () => (
    <span
      title="Computed from a synthetic placeholder transcript — boilerplate text that reads the same for every company, not an earnings call. Illustrative only. Live transcripts are a third-party scrape, off unless the server opts in; when on and the page is unavailable, the engine falls back to this placeholder."
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-yellow-500/10 text-yellow-500/90 border border-yellow-500/30"
    >
      <AlertTriangle className="w-2.5 h-2.5" aria-hidden="true" />
      Synthetic
    </span>
  );

  // While the engine calls are in flight no figure is shown at all. A null figure
  // is one the engine did not report, or one whose call failed; each says so.
  const pending = mode === "loading";
  const show = (n: Figure, f: (v: number) => string, part: Part) =>
    pending ? "…" : n == null ? (missing(part) ? "Unavailable" : "Not reported") : f(n);
  const fmtPct = (n: Figure, part: Part) => show(n, pct, part);
  const fmtUsd = (n: Figure, part: Part) => show(n, usd, part);
  const fmtMoney = (n: Figure, part: Part) => show(n, money, part);
  // Nothing to show for this ticker: the page states why, and no panel renders.
  const noData = mode === "empty" || mode === "uncovered";

  return (
    <main className="min-h-screen">
      <header className="border-b border-border bg-panel/30 backdrop-blur sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4 flex flex-wrap gap-3 items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 bg-gradient-to-br from-purple-500 to-accent-blue rounded-lg flex items-center justify-center glow">
              <FileText className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">FinSight</h1>
              <p className="text-xs text-gray-500">Automated Equity Research</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <ModeBadge />
            <a href="https://github.com/ROROK20/FinSight" className="flex items-center gap-2 px-3 py-1.5 text-xs border border-border hover:border-accent-blue rounded-md">
              <Github className="w-3.5 h-3.5" /> View Source
            </a>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-8">
        {/* Search */}
        <div className="bg-panel border border-border rounded-lg p-4 mb-6 flex gap-3 items-center">
          <Search className="w-5 h-5 text-gray-500" aria-hidden="true" />
          <label htmlFor="ticker-search" className="sr-only">
            Stock ticker symbol
          </label>
          <input
            id="ticker-search"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value.toUpperCase().trim())}
            onKeyDown={(e) => e.key === "Enter" && query && setSelected(query)}
            placeholder="Enter ticker (e.g. AAPL)"
            aria-label="Stock ticker symbol"
            className="flex-1 min-w-0 bg-transparent outline-none text-lg tracking-wider"
          />
          <button
            onClick={() => query && setSelected(query)}
            aria-label={`Analyze ${query || "ticker"}`}
            className="px-4 py-2 bg-accent-blue/20 hover:bg-accent-blue/30 text-accent-blue rounded-md text-sm font-semibold transition-colors"
          >
            Analyze
          </button>
        </div>

        {demoScope(engine) && (
          <p className="-mt-4 mb-4 text-xs text-gray-500">
            Offline demo engine: bundled SEC data for {demoScope(engine)!.join(", ")} only — other
            tickers are not looked up.
          </p>
        )}

        {mode === "recorded" && (
          <div
            role="status"
            className="mb-6 rounded-lg border border-accent-blue/40 bg-accent-blue/10 p-4 text-sm text-blue-200"
          >
            <strong>A recording, not live data.</strong>{" "}
            {noDataReason(engineSaid)}, so
            this page replays the FinSight engine&apos;s own answers for {data.ticker}: computed from
            bundled SEC filing snapshots (retrieved {RECORDING.snapshot_retrieved}) and captured{" "}
            {RECORDING.captured}. Nothing is fetched. Recorded for {RECORDED_TICKERS.join(", ")}; run the
            API for another company that files 10-Ks with the SEC.
          </div>
        )}

        {mode === "empty" && (
          <div
            role="status"
            className="mb-6 rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-4 text-sm text-yellow-200"
          >
            <strong>No data for {data.ticker}.</strong>{" "}
            {noDataReason(engineSaid)}, and
            the recording of the engine holds only {RECORDED_TICKERS.join(", ")}, so nothing has read{" "}
            {data.ticker}&apos;s filings and no figure is shown. To analyse another company that files
            10-Ks, run the API without <code>FINSIGHT_DEMO</code> and with{" "}
            <code>SEC_EDGAR_USER_AGENT</code> set to your own name and email. {LIVE_SCOPE}
          </div>
        )}

        {mode === "uncovered" && (
          <div
            role="status"
            className="mb-6 rounded-lg border border-orange-500/40 bg-orange-500/10 p-4 text-sm text-orange-200"
          >
            <strong>{data.ticker} is not in the offline demo set</strong> (
            {demoScope(engine)?.join(", ")}). The engine runs in demo mode (
            <code>FINSIGHT_DEMO=1</code>), which makes no network requests, so {data.ticker} was not
            looked up and no figure is shown. To analyse another company that files 10-Ks, run the API
            without <code>FINSIGHT_DEMO</code> and with <code>SEC_EDGAR_USER_AGENT</code> set to your
            own name and email. {LIVE_SCOPE}
          </div>
        )}

        <div className="flex gap-2 mb-6 flex-wrap">
          <span className="text-xs text-gray-500 self-center mr-2">Quick:</span>
          {RECORDED_TICKERS.map((t) => (
            <button
              key={t}
              onClick={() => { setQuery(t); setSelected(t); }}
              aria-label={`Analyze ${t}`}
              aria-pressed={selected === t}
              className={clsx(
                "px-3 py-1 text-xs rounded border transition-colors",
                selected === t
                  ? "bg-accent-blue/20 border-accent-blue text-accent-blue"
                  : "border-border hover:border-accent-blue text-gray-400",
              )}
            >
              {t}
            </button>
          ))}
        </div>

        {!noData && (
        <>
        {/* Hero card */}
        <div className="bg-gradient-to-br from-panel to-panel/50 border border-border rounded-lg p-6 mb-6">
          <div className="flex items-start justify-between flex-wrap gap-4">
            <div>
              <div className="flex items-center gap-3 mb-1">
                <h2 className="text-3xl font-bold tracking-tight">{data.ticker}</h2>
                {data.sector && (
                  <span className="text-xs text-gray-500 border border-border px-2 py-0.5 rounded">{data.sector}</span>
                )}
              </div>
              <p className="text-gray-400 mb-4">{data.companyName}</p>
              <div className="flex flex-wrap gap-x-8 gap-y-3">
                <div>
                  <div className="text-xs text-gray-500 uppercase tracking-wider flex items-center gap-1.5">
                    Price {missing("meta") && <UnavailableTag what="The price" />}
                  </div>
                  <div className="text-2xl font-bold tabular-nums">{fmtUsd(data.currentPrice, "meta")}</div>
                  {data.priceDate && (
                    <div className="text-[10px] text-gray-500">close {data.priceDate} · not a live quote</div>
                  )}
                </div>
                <div>
                  <div className="text-xs text-gray-500 uppercase tracking-wider flex items-center gap-1.5">
                    DCF model value {missing("dcf") && <UnavailableTag what="The DCF model value" />}
                  </div>
                  <div className="text-2xl font-bold tabular-nums text-accent-blue">
                    {fmtUsd(data.modelValue, "dcf")}
                  </div>
                  {!pending && data.dcfRefusal && (
                    <div role="note" className="max-w-xs text-[10px] text-orange-300">
                      No DCF value: {data.dcfRefusal}
                    </div>
                  )}
                  {!pending && data.dcfAssumed && data.dcfAssumed.length > 0 && (
                    <div role="note" className="max-w-xs text-[10px] text-orange-300">
                      Rests on figures the filing did not report, assumed by the model:{" "}
                      {data.dcfAssumed.join(", ")}.
                    </div>
                  )}
                  {!pending && data.dcfLimitations?.current_debt && (
                    <div role="note" className="max-w-xs text-[10px] text-orange-300">
                      Current debt may be missing from this value: see What the DCF&apos;s inputs
                      cannot show, below.
                    </div>
                  )}
                  {!pending && data.dcfLimitations?.share_count && (
                    <div role="note" className="max-w-xs text-[10px] text-orange-300">
                      The year-end share count would lower this value: see What the DCF&apos;s
                      inputs cannot show, below.
                    </div>
                  )}
                </div>
                <div>
                  <div className="text-xs text-gray-500 uppercase tracking-wider">Model vs price</div>
                  <div
                    className="text-2xl font-bold tabular-nums text-gray-200"
                    title={
                      data.modelVsPrice == null && !pending
                        ? "Needs both a reported price and a DCF model value from the engine."
                        : undefined
                    }
                  >
                    {!pending && data.modelVsPrice != null && data.modelVsPrice >= 0 ? "+" : ""}
                    {pending ? "…" : data.modelVsPrice == null ? "—" : pct(data.modelVsPrice)}
                  </div>
                  {!pending && data.gapWithheld && (
                    <div role="note" className="max-w-xs text-[10px] text-orange-300">
                      {data.gapWithheld}
                    </div>
                  )}
                  {!pending && data.modelVsPrice == null && !data.gapWithheld && (
                    <div className="max-w-xs text-[10px] text-gray-500">
                      {missing("meta") || missing("dcf")
                        ? "Not compared: an engine call behind it failed."
                        : "Not compared: it needs both a reported price and a DCF model value."}
                    </div>
                  )}
                  {data.valuationDate && (
                    <div
                      className="text-[10px] text-gray-500"
                      title="The DCF is struck at the base fiscal year-end (cash flows discounted to it, net debt as of it) and is not rolled forward, so the gap compares values on two different dates."
                    >
                      valuation date {data.valuationDate} · price{" "}
                      {data.priceDate ? `close ${data.priceDate}` : "latest quote"}
                    </div>
                  )}
                </div>
              </div>
              {missing("dcf") ? (
                <p className="mt-3 max-w-md text-[11px] text-gray-500">
                  The /dcf call failed, so there is no model value here. Nothing stands in for it.
                </p>
              ) : data.dcfRefusal ? null : (
                <p className="mt-3 max-w-md text-[11px] text-gray-500">
                  A mechanical DCF output from one fiscal year&apos;s reported figures at the
                  engine&apos;s assumptions (by default a generic 10% WACC and 3% terminal growth) — not
                  a price target, a rating or investment advice.
                </p>
              )}
            </div>
            <div className="flex-1 max-w-md text-sm text-gray-300 leading-relaxed">
              <div className="text-xs text-gray-500 uppercase tracking-wider mb-2 flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> Summary
              </div>
              {pending ? "Loading…" : data.investmentThesis}
            </div>
          </div>
        </div>

        {/* Financial Summary */}
        <div className="flex items-center gap-2 mb-2 text-xs text-gray-500">
          {data.fiscalYear ? `Financials · ${data.fiscalYear} 10-K (SEC EDGAR)` : "Financials"}
          {missing("kpis") && <UnavailableTag what="The financial summary" />}
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 mb-6">
          {[
            { label: `Revenue ${data.fiscalYear ?? "(FY)"}`, value: fmtMoney(data.revenueTTM, "kpis") },
            {
              label: "Revenue Growth",
              value: fmtPct(data.revenueGrowth, "kpis"),
              trend:
                pending || data.revenueGrowth == null ? undefined : data.revenueGrowth > 0 ? "up" : "down",
            },
            { label: "Gross Margin", value: fmtPct(data.grossMargin, "kpis") },
            { label: "Op Margin", value: fmtPct(data.operatingMargin, "kpis") },
            { label: "Net Margin", value: fmtPct(data.netMargin, "kpis") },
            {
              label: "Reported FCF",
              value: fmtMoney(data.fcf, "kpis"),
              title: "Operating cash flow − CapEx, from the 10-K. Not the DCF's projected FCF, which starts from operating income and so keeps stock comp as a cost.",
            },
            { label: "EPS (Diluted)", value: fmtUsd(data.eps, "kpis") },
          ].map((m) => (
            <div key={m.label} className="metric-card">
              <div className="text-xs uppercase tracking-wider text-gray-500 mb-1" title={m.title}>{m.label}</div>
              <div className={clsx(
                "text-lg font-bold tabular-nums",
                m.trend === "up" && "text-accent-green",
                m.trend === "down" && "text-accent-red",
              )}>{m.value}</div>
            </div>
          ))}
        </div>

        {/* The DCF's explicit years: engine output, from the /dcf response */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
          {([
            { key: "revenue", title: "DCF projection: revenue", fill: "#3b82f6", note: "" },
            {
              key: "fcf",
              title: "DCF projection: modelled FCF",
              fill: "#10b981",
              note: " · EBIT × (1 − tax) + D&A − CapEx − ΔNWC: stock comp stays a cost, unlike Reported FCF above (operating cash flow − CapEx)",
            },
          ] as const).map((chart) => (
            <div key={chart.key} className="bg-panel border border-border rounded-lg p-4">
              <h3 className="text-sm font-semibold mb-1 flex items-center gap-2">
                <BarChart3 className="w-4 h-4" /> {chart.title}
                {missing("dcf") && <UnavailableTag what="The DCF projection" />}
              </h3>
              <p className="text-xs text-gray-500 mb-3">
                $M, per year after the valuation date · the model&apos;s output at the stated
                assumptions, not a forecast and not reported history{chart.note}
              </p>
              {data.projection.length > 0 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={data.projection}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1c2540" />
                    <XAxis dataKey="period" stroke="#6b7280" tick={{ fontSize: 11 }} />
                    <YAxis stroke="#6b7280" tick={{ fontSize: 11 }} tickFormatter={(v) => `${(v / 1000).toFixed(0)}B`} />
                    <Tooltip contentStyle={{ background: "#0f1525", border: "1px solid #1c2540", fontSize: "0.75rem" }}
                      formatter={(v: number) => `$${Math.round(v).toLocaleString()}M`} />
                    <Bar dataKey={chart.key} fill={chart.fill} radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-8 text-center text-xs text-gray-500">
                  {pending ? "…" : "No projection: the engine returned no DCF for this ticker."}
                </p>
              )}
            </div>
          ))}
        </div>

        {/* DCF, sentiment, and what this dashboard does not show */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
          <div className="bg-panel border border-border rounded-lg p-5">
            <h3 className="text-sm font-semibold flex items-center gap-2 mb-1">
              <DollarSign className="w-4 h-4" /> DCF Valuation
              {missing("dcf") && <UnavailableTag what="The DCF valuation" />}
            </h3>
            <p className="text-xs text-gray-500 mb-3">
              {data.dcfRefusal
                ? "Not valued: the engine refused this ticker"
                : data.dcfBasis
                  ? `Basis: ${data.dcfBasis}`
                  : pending
                    ? "…"
                    : "Not valued: the /dcf call failed"}
              {data.dcfDefaults && data.dcfDefaults.length > 0 && (
                <span title={`Model defaults, not reported figures: ${data.dcfDefaults.join(", ")}`}>
                  {" "}· {data.dcfDefaults.length} model default{data.dcfDefaults.length === 1 ? "" : "s"}
                </span>
              )}
              {data.dcfAdjustments && Object.keys(data.dcfAdjustments).length > 0 && (
                <span
                  title={`Reported figures the projection adjusts:\n${Object.entries(data.dcfAdjustments)
                    .map(([k, v]) => `• ${k}: ${v}`)
                    .join("\n")}`}
                >
                  {" "}· {Object.keys(data.dcfAdjustments).length} adjustment
                  {Object.keys(data.dcfAdjustments).length === 1 ? "" : "s"}
                </span>
              )}
            </p>
            {data.dcfRefusal && (
              <p role="note" className="-mt-1 mb-3 text-[11px] text-orange-300">
                {data.dcfRefusal}
              </p>
            )}
            {data.dcfAssumed && data.dcfAssumed.length > 0 && (
              <p role="note" className="-mt-1 mb-3 text-[11px] text-orange-300">
                Rests on figures the filing did not report, assumed by the model:{" "}
                {data.dcfAssumed.join(", ")}.
              </p>
            )}
            <div className="space-y-2 text-sm">
              <div className="flex justify-between border-b border-border pb-2">
                <span className="text-gray-500">Intrinsic Value</span>
                <span className="font-bold tabular-nums">{fmtUsd(data.dcfIntrinsicValue, "dcf")}</span>
              </div>
              <div className="flex justify-between border-b border-border pb-2">
                <span className="text-gray-500">Enterprise Value</span>
                <span className="font-bold tabular-nums">{fmtMoney(data.dcfEV, "dcf")}</span>
              </div>
              <div className="flex justify-between border-b border-border pb-2">
                <span
                  className="text-gray-500"
                  title="Total debt − cash and cash equivalents; marketable securities are not netted. Enterprise value − net debt = equity value; equity value ÷ shares = the value per share."
                >
                  Net Debt
                </span>
                <span className="font-bold tabular-nums">{fmtMoney(data.dcfNetDebt, "dcf")}</span>
              </div>
              <div className="flex justify-between border-b border-border pb-2">
                <span className="text-gray-500">Equity Value</span>
                <span className="font-bold tabular-nums">{fmtMoney(data.dcfEquity, "dcf")}</span>
              </div>
              <div className="flex justify-between border-b border-border pb-2">
                <span className="text-gray-500">Shares</span>
                <span className="font-bold tabular-nums">
                  {show(data.dcfShares, (v) => `${v.toLocaleString("en-US", { maximumFractionDigits: 1 })}M`, "dcf")}
                </span>
              </div>
              <div className="flex justify-between border-b border-border pb-2">
                <span className="text-gray-500">WACC</span>
                <span className="font-bold tabular-nums">{fmtPct(data.wacc, "dcf")}</span>
              </div>
              <div className="flex justify-between pb-2">
                <span className="text-gray-500">Terminal Growth</span>
                <span className="font-bold tabular-nums">{fmtPct(data.terminalGrowth, "dcf")}</span>
              </div>
            </div>
          </div>

          <div className="bg-panel border border-border rounded-lg p-5">
            <h3 className="text-sm font-semibold mb-3 flex items-center gap-2">
              Earnings Sentiment
              {missing("sentiment") && <UnavailableTag what="The sentiment panel" />}
              {!missing("sentiment") && data.sentimentSynthetic && <SyntheticTag />}
            </h3>
            <div className="text-center mb-4">
              <div className={clsx(
                "text-3xl font-bold mb-1",
                data.sentimentLabel === "Bullish" && "text-accent-green",
                data.sentimentLabel === "Bearish" && "text-accent-red",
                data.sentimentLabel === "Neutral" && "text-gray-400",
              )}>{pending ? "…" : data.sentimentLabel ?? "Unavailable"}</div>
              <div className="text-xs text-gray-500">
                Net score:{" "}
                {show(data.sentimentScore, (v) => `${v >= 0 ? "+" : ""}${v.toFixed(3)}`, "sentiment")}
              </div>
            </div>
            {!missing("sentiment") && data.sentimentSynthetic && (
              <p className="mb-3 text-[11px] text-gray-500">
                No real earnings call was analysed: these scores come from a boilerplate placeholder
                that reads the same for every company. Live transcripts are a third-party scrape, off
                unless the server opts in, and the page it reads may be unavailable.
              </p>
            )}
            {!pending && data.sentimentUnavailable && (
              <p role="note" className="mb-3 text-[11px] text-gray-500">
                {data.sentimentUnavailable}
              </p>
            )}
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">Guidance</span>
                <span
                  title="Only an explicit action on guidance counts (raised, lowered, reaffirmed, initiated, withdrawn); results compared with guidance are not a change."
                  className={clsx(
                    "font-semibold",
                    data.guidanceTone === "raised" && "text-accent-green",
                    data.guidanceTone === "lowered" && "text-accent-red",
                  )}
                >
                  {pending ? "…" : data.guidanceTone ? GUIDANCE_COPY[data.guidanceTone] : "Unavailable"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Risk Mentions</span>
                <span className="font-semibold">{show(data.riskMentions, String, "sentiment")}</span>
              </div>
            </div>
          </div>

          <div className="bg-panel border border-border rounded-lg p-5">
            <h3 className="text-sm font-semibold flex items-center gap-2 mb-3">
              <Sparkles className="w-4 h-4" /> Not on this dashboard
            </h3>
            <p className="text-xs text-gray-400 leading-relaxed">
              Peer multiples, multi-year history, catalysts and risks: the API serves no endpoint for
              them here, so this page shows none rather than invent them. Peer multiples are in the
              live <code>/report</code>, for tickers with a curated peer set, from Yahoo Finance. Another
              fiscal year is one call away: <code>/kpis/{"{ticker}"}?period=YYYY</code>.
            </p>
          </div>
        </div>

        {limitationRows(data.dcfLimitations).length > 0 && (
          <div className="bg-panel border border-border rounded-lg p-5 mb-6">
            <h3 className="text-sm font-semibold flex items-center gap-2 mb-3">
              <AlertTriangle className="w-4 h-4" aria-hidden="true" /> What the DCF&apos;s inputs
              cannot show
            </h3>
            <ul className="space-y-2 text-xs text-gray-400 leading-relaxed">
              {limitationRows(data.dcfLimitations).map((row) => (
                <li key={row.key}>
                  <strong className="text-gray-300">{row.label}:</strong>{" "}
                  {linkParts(row.text).map((part, i) =>
                    part.href ? (
                      <a
                        key={i}
                        href={part.href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="break-all underline underline-offset-2 hover:text-accent-blue"
                      >
                        {part.text}
                      </a>
                    ) : (
                      <span key={i}>{part.text}</span>
                    ),
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        </>
        )}

        <footer className="mt-12 pt-6 border-t border-border text-xs text-gray-500 flex justify-between">
          <p>
            FinSight · equity research from SEC filings ·{" "}
            <a
              href="https://github.com/ROROK20/FinSight"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-accent-blue underline-offset-2 hover:underline"
            >
              GitHub
            </a>{" "}
            ·{" "}
            <a
              href="https://github.com/ROROK20/FinSight/blob/main/LICENSE"
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-accent-blue underline-offset-2 hover:underline"
            >
              MIT License
            </a>{" "}
            ·{" "}
            {modeCopy(mode, engineSaid).footer}
          </p>
          <p>v0.1.0</p>
        </footer>
      </div>
    </main>
  );
}

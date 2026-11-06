"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  emptyResearch, limitationRows, linkParts, mergeEngineData, recordedResearch,
  type CompanyResearch, type Figure, type GuidanceTone, type Recording,
} from "@/lib/research";
import recordedDemo from "@/lib/recordedDemo.json";
import { api, tagged, demoScope, engineProblem, noDataReason, type HealthResponse } from "@/lib/api";
import { fmtMillions, fmtMoney as money, fmtPct as pct, fmtUsd as usd } from "@/lib/format";
import { buildNotes, type NoteKey, type NoteSheet } from "@/lib/notes";
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
  { label: string; cls: string; title: string; footer: string }
> = {
  live: {
    label: "Live data",
    cls: "slug-ink",
    title:
      "Headline financials and the DCF come from the live FinSight engine (SEC EDGAR 10-K data). A panel tagged Synthetic was computed from a placeholder transcript.",
    footer: "Live data from the FinSight engine · a panel tagged Synthetic used a placeholder transcript",
  },
  demo: {
    label: "Demo data",
    cls: "slug-ink",
    title:
      "Backend reachable, serving bundled SEC filings and dated prices through the real engine. The panel tagged Synthetic was computed from a placeholder transcript.",
    footer: "Demo: bundled SEC filings via the real engine · the panel tagged Synthetic used a placeholder transcript",
  },
  partial: {
    label: "Partial data",
    cls: "slug-oxblood",
    title: "Some engine calls failed. Their panels read Unavailable; the rest come from the engine.",
    footer: "Partial — panels reading Unavailable had a failed engine call; nothing replaces them",
  },
  recorded: {
    label: "Engine recording · bundled SEC snapshots · no live backend",
    cls: "slug-ink",
    title: `A recording of the FinSight engine running on its bundled SEC snapshots (retrieved ${RECORDING.snapshot_retrieved}), captured ${RECORDING.captured}. No live backend answered, so nothing was fetched.`,
    footer: `Recording of the FinSight engine on bundled SEC snapshots, captured ${RECORDING.captured} · no live backend`,
  },
  empty: {
    label: "Offline · no data",
    cls: "slug-oxblood",
    title: "No live backend answered and the recording does not hold this ticker, so no figure is shown.",
    footer: "Offline — no live backend, and this ticker is not in the recording, so no figure is shown",
  },
  uncovered: {
    label: "Not in demo set",
    cls: "slug-oxblood",
    title:
      "The engine is in offline demo mode, which covers only its bundled tickers and makes no network requests. This ticker was not looked up, so no figure is shown.",
    footer: "Demo engine — this ticker is not bundled, so nothing was looked up and no figure is shown",
  },
  loading: {
    label: "Loading engine…",
    cls: "slug-quiet",
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

/** Marks a panel whose engine call failed. No figure stands in for it. */
const UnavailableTag = ({ what }: { what: string }) => (
  <span
    title={`${what}: the engine call behind it failed or returned an unverifiable response, so nothing is shown.`}
    className="tag"
  >
    Unavailable
  </span>
);

/** Marks the sentiment panel when it was computed from the placeholder transcript. */
const SyntheticTag = () => (
  <span
    title="Computed from a synthetic placeholder transcript: boilerplate text that reads the same for every company, not an earnings call. Illustrative only. Live transcripts are a third-party scrape, off unless the server opts in; when on and the page is unavailable, the engine falls back to this placeholder."
    className="tag"
  >
    Synthetic
  </span>
);

/** A numbered footnote marker, present only when the engine's response carries a note for the figure. */
const Fn = ({ sheet, k, raised }: { sheet: NoteSheet; k: NoteKey; raised?: boolean }) => {
  const n = sheet.number[k];
  if (n == null) return null;
  return (
    <a id={`ref-${k}`} href={`#note-${k}`} aria-label={`Note ${n}`} className={clsx("fn", raised && "fn-sup")}>
      {n}
    </a>
  );
};

/** One step of the DCF: a dt and its dds, so the page reads label, value, $M, note. */
const Step = ({
  op, label, title, value, millions, sheet, note, total,
}: {
  op?: string;
  label: string;
  title?: string;
  value: string;
  millions?: Figure;
  sheet: NoteSheet;
  note?: NoteKey;
  total?: boolean;
}) => (
  <div className={clsx("step", total && "step-total")}>
    <dt title={title}>
      {op && <span className="step-op" aria-hidden="true">{op}</span>}
      {label}
    </dt>
    <dd className="figure">{value}</dd>
    <dd className="worked">{millions == null ? "" : fmtMillions(millions)}</dd>
    <dd>{note && <Fn sheet={sheet} k={note} />}</dd>
  </div>
);

/** Dates, accession numbers and "10-K" stay whole: a line must not break one at its hyphen. */
const Whole = ({ text }: { text: string }) => (
  <>
    {text.split(/(\d{10}-\d{2}-\d{6}|\d{4}-\d{2}-\d{2}|\b10-K\b)/).map((part, i) =>
      i % 2 ? <span key={i} className="whitespace-nowrap">{part}</span> : part,
    )}
  </>
);

/** A ratio's working in $M, shown only when the two figures really give the ratio. */
function working(top: Figure | undefined, bottom: Figure, ratio: Figure): string | null {
  if (top == null || bottom == null || ratio == null || bottom === 0) return null;
  if (Math.abs(top / bottom - ratio) > 1e-6) return null;
  return `${fmtMillions(top)} ÷ ${fmtMillions(bottom)}`;
}

/**
 * The sentence the note opens on. Every value in it is one the header block below
 * renders, and it always says what the model value is. Each way the engine can
 * answer has its own sentence, so a failed or refused DCF never reads as a value.
 */
function openingSentence(d: CompanyResearch, pending: boolean, dcfFailed: boolean): string {
  if (pending) return `Contacting the FinSight engine for ${d.ticker}…`;
  const name = d.companyName;
  if (d.dcfRefusal) {
    return `The engine refused to value ${name}, so this note has no model value.`;
  }
  if (d.modelValue == null) {
    return dcfFailed
      ? `The DCF call for ${name} failed, so this note has no model value.`
      : `The engine reported no DCF model value for ${name}.`;
  }
  const value = `${name}’s DCF model value is ${usd(d.modelValue)} a share`;
  const source = d.fiscalYear ? `its ${d.fiscalYear} 10-K` : "one fiscal year’s reported figures";
  const what = `a mechanical output of ${source} at the engine’s assumptions, not a price target`;
  if (d.modelVsPrice != null && d.currentPrice != null) {
    const gap = `${pct(Math.abs(d.modelVsPrice))} ${d.modelVsPrice >= 0 ? "above" : "below"}`;
    const close = d.priceDate ? `close of ${d.priceDate}` : "latest quote";
    return `${value}, ${gap} the ${usd(d.currentPrice)} ${close}: ${what}.`;
  }
  return `${value}: ${what}. It is not compared with a price here.`;
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

  const badge = modeCopy(mode, engineSaid);

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
  const sheet = buildNotes(data);
  const limitations = limitationRows(data.dcfLimitations);

  return (
    <main className="min-h-screen border-t-[6px] border-oxblood">
      <header className="border-b-2 border-ink">
        <div className="sheet flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-2.5">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <h1 className="text-[1.375rem] font-semibold leading-tight">FinSight</h1>
            <p className="stat text-[0.9375rem] text-ink-soft">Automated Equity Research</p>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span title={badge.title} className={clsx("slug", badge.cls)}>
              {badge.label}
            </span>
            <a href="https://github.com/ROROK20/FinSight" className="link stat inline-flex min-h-6 items-center text-[0.9375rem]">
              View Source
            </a>
          </div>
        </div>
      </header>

      <div className="sheet pb-8">
        {/* Ticker controls */}
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            <label htmlFor="ticker-search" className="label">
              Ticker
            </label>
            <input
              id="ticker-search"
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value.toUpperCase().trim())}
              onKeyDown={(e) => e.key === "Enter" && query && setSelected(query)}
              placeholder="Enter ticker (e.g. AAPL)"
              className="field w-44"
            />
            <button
              onClick={() => query && setSelected(query)}
              aria-label={`Analyze ${query || "ticker"}`}
              className="button"
            >
              Analyze
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-x-1 gap-y-1">
            <span className="label mr-1">Quick:</span>
            {RECORDED_TICKERS.map((t) => (
              <button
                key={t}
                onClick={() => { setQuery(t); setSelected(t); }}
                aria-label={`Analyze ${t}`}
                aria-pressed={selected === t}
                className="quick"
              >
                {t}
              </button>
            ))}
          </div>
          {demoScope(engine) && (
            <p className="fineprint">
              Offline demo engine: bundled SEC data for {demoScope(engine)!.join(", ")} only — other
              tickers are not looked up.
            </p>
          )}
        </div>

        {mode === "recorded" && (
          <div role="status" className="notice mb-4">
            <strong>A recording, not live data.</strong>{" "}
            {noDataReason(engineSaid)}, so
            this page replays the FinSight engine&apos;s own answers for {data.ticker}: computed from
            bundled SEC filing snapshots (retrieved {RECORDING.snapshot_retrieved}) and captured{" "}
            {RECORDING.captured}. Nothing is fetched. Recorded for {RECORDED_TICKERS.join(", ")}; run the
            API for another company that files 10-Ks with the SEC.
          </div>
        )}

        {mode === "empty" && (
          <div role="status" className="notice notice-stop mb-4">
            <strong>No data for {data.ticker}.</strong>{" "}
            {noDataReason(engineSaid)}, and
            the recording of the engine holds only {RECORDED_TICKERS.join(", ")}, so nothing has read{" "}
            {data.ticker}&apos;s filings and no figure is shown. To analyse another company that files
            10-Ks, run the API without <code>FINSIGHT_DEMO</code> and with{" "}
            <code>SEC_EDGAR_USER_AGENT</code> set to your own name and email. {LIVE_SCOPE}
          </div>
        )}

        {mode === "uncovered" && (
          <div role="status" className="notice notice-stop mb-4">
            <strong>{data.ticker} is not in the offline demo set</strong> (
            {demoScope(engine)?.join(", ")}). The engine runs in demo mode (
            <code>FINSIGHT_DEMO=1</code>), which makes no network requests, so {data.ticker} was not
            looked up and no figure is shown. To analyse another company that files 10-Ks, run the API
            without <code>FINSIGHT_DEMO</code> and with <code>SEC_EDGAR_USER_AGENT</code> set to your
            own name and email. {LIVE_SCOPE}
          </div>
        )}

        {!noData && (
        <>
        {/* Hero card */}
        <div>
          <p className="lede mb-3.5 mt-1">
            <Whole text={openingSentence(data, pending, missing("dcf"))} />
          </p>
          <div className="strip">
            <div className="strip-cell">
              <h2 className="text-[1.375rem] font-semibold leading-tight">{data.companyName}</h2>
              {(data.companyName !== data.ticker || data.sector) && (
                <p className="stat mt-0.5 text-[0.9375rem] text-ink-soft">
                  {data.companyName !== data.ticker && data.ticker}
                  {data.companyName !== data.ticker && data.sector && " · "}
                  {data.sector}
                </p>
              )}
            </div>
            <div className="strip-cell">
              <div className="label flex flex-wrap items-center gap-1.5">
                Price {missing("meta") && <UnavailableTag what="The price" />}
              </div>
              <div className="strip-figure mt-0.5">
                {fmtUsd(data.currentPrice, "meta")}
                <Fn sheet={sheet} k="price" raised />
              </div>
              {data.priceDate && (
                <div className="fineprint mt-0.5">close {data.priceDate} · not a live quote</div>
              )}
            </div>
            <div className="strip-cell">
              <div className="label flex flex-wrap items-center gap-1.5">
                DCF model value {missing("dcf") && <UnavailableTag what="The DCF model value" />}
              </div>
              <div className="strip-figure mt-0.5">
                {fmtUsd(data.modelValue, "dcf")}
                <Fn sheet={sheet} k="model" raised />
              </div>
              {!pending && data.dcfRefusal && (
                <div role="note" className="caveat mt-0.5">
                  No DCF value: {data.dcfRefusal}
                </div>
              )}
              {!pending && data.dcfAssumed && data.dcfAssumed.length > 0 && (
                <div role="note" className="caveat mt-0.5">
                  Rests on figures the filing did not report, assumed by the model:{" "}
                  {data.dcfAssumed.join(", ")}.
                </div>
              )}
              {!pending && data.dcfLimitations?.current_debt && (
                <div role="note" className="caveat mt-0.5">
                  Current debt may be missing from this value: see{" "}
                  <a href="#limitations" className="link">What the DCF&apos;s inputs cannot show</a>,
                  below.
                </div>
              )}
              {!pending && data.dcfLimitations?.share_count && (
                <div role="note" className="caveat mt-0.5">
                  The year-end share count would lower this value: see{" "}
                  <a href="#limitations" className="link">What the DCF&apos;s inputs cannot show</a>,
                  below.
                </div>
              )}
            </div>
            <div className="strip-cell">
              <div className="label">Model vs price</div>
              <div
                className="strip-figure mt-0.5"
                title={
                  data.modelVsPrice == null && !pending
                    ? "Needs both a reported price and a DCF model value from the engine."
                    : undefined
                }
              >
                {!pending && data.modelVsPrice != null && data.modelVsPrice >= 0 ? "+" : ""}
                {pending ? "…" : data.modelVsPrice == null ? "n/a" : pct(data.modelVsPrice)}
                <Fn sheet={sheet} k="gap" raised />
              </div>
              {!pending && data.gapWithheld && (
                <div role="note" className="caveat mt-0.5">
                  {data.gapWithheld}
                </div>
              )}
              {!pending && data.modelVsPrice == null && !data.gapWithheld && (
                <div className="fineprint mt-0.5">
                  {missing("meta") || missing("dcf")
                    ? "Not compared: an engine call behind it failed."
                    : "Not compared: it needs both a reported price and a DCF model value."}
                </div>
              )}
              {data.valuationDate && (
                <div
                  className="fineprint mt-0.5"
                  title="The DCF is struck at the base fiscal year-end (cash flows discounted to it, net debt as of it) and is not rolled forward, so the gap compares values on two different dates."
                >
                  valuation date {data.valuationDate} · price{" "}
                  {data.priceDate ? `close ${data.priceDate}` : "latest quote"}
                </div>
              )}
            </div>
          </div>
          <div className="mt-2.5 grid gap-x-10 gap-y-1.5 lg:grid-cols-2">
            <p className="prose-note">
              <strong className="font-semibold">Summary.</strong>{" "}
              {pending ? "Loading…" : data.investmentThesis}
            </p>
            {missing("dcf") ? (
              <p className="prose-note text-ink-soft">
                The /dcf call failed, so there is no model value here. Nothing stands in for it.
              </p>
            ) : data.dcfRefusal ? null : (
              <p className="prose-note text-ink-soft">
                A mechanical DCF output from one fiscal year&apos;s reported figures at the
                engine&apos;s assumptions (by default a generic 10% WACC and 3% terminal growth) — not
                a price target, a rating or investment advice.
              </p>
            )}
          </div>
        </div>

        {/* The statistical array: the reported lines, and the DCF worked from them */}
        <div data-testid="financial-array" className="mt-5 grid gap-x-10 gap-y-6 lg:grid-cols-2">
          <section aria-labelledby="financials-head">
            <h3 id="financials-head" className="sect">
              {data.fiscalYear ? `Financials · ${data.fiscalYear} 10-K (SEC EDGAR)` : "Financials"}
              {missing("kpis") && <UnavailableTag what="The financial summary" />}
            </h3>
            <div className="scroll-x" tabIndex={0} role="region" aria-labelledby="financials-head">
              <table className="array">
                <thead>
                  <tr>
                    <th scope="col">Line</th>
                    <th scope="col" className="num">{data.fiscalYear ?? "FY"}</th>
                    <th scope="col" className="num">10-K, $M</th>
                    <th scope="col" className="mark"><span className="sr-only">Note</span></th>
                  </tr>
                </thead>
                <tbody>
                  {([
                    {
                      label: `Revenue ${data.fiscalYear ?? "(FY)"}`,
                      value: fmtMoney(data.revenueTTM, "kpis"),
                      filed: data.revenueTTM == null ? null : fmtMillions(data.revenueTTM),
                      note: "revenue",
                    },
                    { label: "Revenue Growth", value: fmtPct(data.revenueGrowth, "kpis"), note: "growth" },
                    {
                      label: "Gross Margin",
                      value: fmtPct(data.grossMargin, "kpis"),
                      filed: working(data.reported?.grossProfit, data.revenueTTM, data.grossMargin),
                      note: "gross",
                    },
                    {
                      label: "Op Margin",
                      value: fmtPct(data.operatingMargin, "kpis"),
                      filed: working(data.reported?.operatingIncome, data.revenueTTM, data.operatingMargin),
                      note: "op",
                    },
                    {
                      label: "Net Margin",
                      value: fmtPct(data.netMargin, "kpis"),
                      filed: working(data.reported?.netIncome, data.revenueTTM, data.netMargin),
                      note: "net",
                    },
                    {
                      label: "Reported FCF",
                      value: fmtMoney(data.fcf, "kpis"),
                      filed: data.fcf == null ? null : fmtMillions(data.fcf),
                      title: "Operating cash flow − CapEx, from the 10-K. Not the DCF's projected FCF, which starts from operating income and so keeps stock comp as a cost.",
                      note: "fcf",
                    },
                    { label: "EPS (Diluted)", value: fmtUsd(data.eps, "kpis"), note: "eps" },
                  ] as { label: string; value: string; filed?: string | null; title?: string; note: NoteKey }[]).map((row) => (
                    <tr key={row.note}>
                      <th scope="row" title={row.title}>{row.label}</th>
                      <td className="num figure">{row.value}</td>
                      <td className="num worked">{row.filed}</td>
                      <td className="mark"><Fn sheet={sheet} k={row.note} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section aria-labelledby="dcf-head">
            <h3 id="dcf-head" className="sect">
              DCF Valuation
              {missing("dcf") && <UnavailableTag what="The DCF valuation" />}
            </h3>
            <p className="fineprint mt-0.5">
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
              <Fn sheet={sheet} k="basis" raised />
            </p>
            {data.dcfRefusal && (
              <p role="note" className="caveat mt-1">
                {data.dcfRefusal}
              </p>
            )}
            {data.dcfAssumed && data.dcfAssumed.length > 0 && (
              <p role="note" className="caveat mt-1">
                Rests on figures the filing did not report, assumed by the model:{" "}
                {data.dcfAssumed.join(", ")}.
              </p>
            )}
            <div className="steps mt-1">
              <div className="step step-head" aria-hidden="true">
                <span>Step</span>
                <span className="text-right">Value</span>
                <span className="text-right">$M</span>
                <span />
              </div>
              <dl>
                <Step label="Enterprise Value" value={fmtMoney(data.dcfEV, "dcf")} millions={data.dcfEV} sheet={sheet} />
                <Step
                  op="−"
                  label="Net Debt"
                  title="Total debt − cash and cash equivalents; marketable securities are not netted. Enterprise value − net debt = equity value; equity value ÷ shares = the value per share."
                  value={fmtMoney(data.dcfNetDebt, "dcf")}
                  millions={data.dcfNetDebt}
                  sheet={sheet}
                  note="netDebt"
                />
                <Step op="=" label="Equity Value" value={fmtMoney(data.dcfEquity, "dcf")} millions={data.dcfEquity} sheet={sheet} />
                <Step
                  op="÷"
                  label="Shares"
                  value={show(data.dcfShares, (v) => `${v.toLocaleString("en-US", { maximumFractionDigits: 1 })}M`, "dcf")}
                  sheet={sheet}
                  note="shares"
                />
                <Step op="=" label="Intrinsic Value" value={fmtUsd(data.dcfIntrinsicValue, "dcf")} sheet={sheet} total />
                <Step label="WACC" value={fmtPct(data.wacc, "dcf")} sheet={sheet} note="wacc" />
                <Step label="Terminal Growth" value={fmtPct(data.terminalGrowth, "dcf")} sheet={sheet} note="terminal" />
              </dl>
            </div>
          </section>
        </div>

        {/* The DCF's explicit years: engine output, from the /dcf response */}
        <div className="plots mt-7">
          {([
            { key: "revenue", title: "DCF projection: revenue", note: "" },
            {
              key: "fcf",
              title: "DCF projection: modelled FCF",
              note: " · EBIT × (1 − tax) + D&A − CapEx − ΔNWC: stock comp stays a cost, unlike Reported FCF above (operating cash flow − CapEx)",
            },
          ] as const).map((chart) => {
            const values = data.projection.map((p) => p[chart.key]);
            const low = Math.min(0, ...values);
            const span = Math.max(0, ...values) - low || 1;
            return (
              <section key={chart.key} aria-labelledby={`projection-${chart.key}`}>
                <h3 id={`projection-${chart.key}`} className="sect">
                  {chart.title}
                  {missing("dcf") && <UnavailableTag what="The DCF projection" />}
                </h3>
                <p className="fineprint mt-0.5">
                  $M, per year after the valuation date · the model&apos;s output at the stated
                  assumptions, not a forecast and not reported history{chart.note}
                </p>
                {data.projection.length > 0 ? (
                  <div className="scroll-x mt-1" tabIndex={0} role="region" aria-labelledby={`projection-${chart.key}`}>
                    <table className="plot">
                      <thead>
                        <tr>
                          {data.projection.map((p) => (
                            <th key={p.period} scope="col">{p.period}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        <tr aria-hidden="true">
                          {values.map((v, i) => (
                            <td key={i}>
                              <div className="plot-well">
                                <div
                                  className="plot-bar"
                                  style={{
                                    bottom: `${((Math.min(v, 0) - low) / span) * 100}%`,
                                    height: `${(Math.abs(v) / span) * 100}%`,
                                  }}
                                />
                                <div className="plot-base" style={{ bottom: `${(-low / span) * 100}%` }} />
                              </div>
                            </td>
                          ))}
                        </tr>
                        <tr>
                          {values.map((v, i) => (
                            <td key={i} className="plot-figure">{fmtMillions(v)}</td>
                          ))}
                        </tr>
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="fineprint py-6 text-center">
                    {pending ? "…" : "No projection: the engine returned no DCF for this ticker."}
                  </p>
                )}
              </section>
            );
          })}
        </div>

        {limitations.length > 0 && (
          <section id="limitations" aria-labelledby="limitations-head" className="mt-7 scroll-mt-4">
            <h3 id="limitations-head" className="sect">What the DCF&apos;s inputs cannot show</h3>
            <ul className="caveats mt-1.5">
              {limitations.map((row) => (
                <li key={row.key} className="prose-note">
                  <strong className="font-semibold">{row.label}:</strong>{" "}
                  {linkParts(row.text).map((part, i) =>
                    part.href ? (
                      <a
                        key={i}
                        href={part.href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="link"
                      >
                        {part.text}
                      </a>
                    ) : (
                      <Whole key={i} text={part.text} />
                    ),
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* Earnings sentiment, and what this note does not show */}
        <div className="mt-7 grid gap-x-10 gap-y-6 lg:grid-cols-2">
          <section aria-labelledby="sentiment-head">
            <h3 id="sentiment-head" className="sect">
              Earnings Sentiment
              {missing("sentiment") && <UnavailableTag what="The sentiment panel" />}
              {!missing("sentiment") && data.sentimentSynthetic && <SyntheticTag />}
            </h3>
            {!missing("sentiment") && data.sentimentSynthetic && (
              <p className="prose-note mt-1">
                No real earnings call was analysed: these scores come from a boilerplate placeholder
                that reads the same for every company. Live transcripts are a third-party scrape, off
                unless the server opts in, and the page it reads may be unavailable.
              </p>
            )}
            {!pending && data.sentimentUnavailable && (
              <p role="note" className="prose-note mt-1">
                {data.sentimentUnavailable}
              </p>
            )}
            <dl className="steps mt-1">
              <div className="step step-pair">
                <dt>Tone</dt>
                <dd className="figure">{pending ? "…" : data.sentimentLabel ?? "Unavailable"}</dd>
              </div>
              <div className="step step-pair">
                <dt>Net score</dt>
                <dd className="figure">
                  {show(data.sentimentScore, (v) => `${v >= 0 ? "+" : ""}${v.toFixed(3)}`, "sentiment")}
                </dd>
              </div>
              <div className="step step-pair">
                <dt>Guidance</dt>
                <dd
                  className="figure"
                  title="Only an explicit action on guidance counts (raised, lowered, reaffirmed, initiated, withdrawn); results compared with guidance are not a change."
                >
                  {pending ? "…" : data.guidanceTone ? GUIDANCE_COPY[data.guidanceTone] : "Unavailable"}
                </dd>
              </div>
              <div className="step step-pair">
                <dt>Risk Mentions</dt>
                <dd className="figure">{show(data.riskMentions, String, "sentiment")}</dd>
              </div>
            </dl>
          </section>

          <section aria-labelledby="absent-head">
            <h3 id="absent-head" className="sect">Not on this dashboard</h3>
            <p className="prose-note mt-1">
              Peer multiples, multi-year history, catalysts and risks: the API serves no endpoint for
              them here, so this page shows none rather than invent them. Peer multiples are in the
              live <code>/report</code>, for tickers with a curated peer set, from Yahoo Finance. Another
              fiscal year is one call away: <code>/kpis/{"{ticker}"}?period=YYYY</code>.
            </p>
          </section>
        </div>

        {sheet.notes.length > 0 && (
          <section id="notes" aria-labelledby="notes-head" className="mt-7">
            <h3 id="notes-head" className="sect">Sources and notes</h3>
            {sheet.filing && (
              <p className="mb-2 mt-1 max-w-[72ch] text-[0.875rem] leading-[1.45]">
                Unless a note names another, the financial figures come from <Whole text={sheet.filing} />.
              </p>
            )}
            <ol className="notes">
              {sheet.notes.map((note) => (
                <li key={note.key} id={`note-${note.key}`}>
                  <span className="note-label">{note.label}.</span> <Whole text={note.text} />{"\u00A0"}
                  <a href={`#ref-${note.key}`} aria-label={`Back to the figure for note ${note.number}`} className="note-back">
                    ↑
                  </a>
                </li>
              ))}
            </ol>
            {data.modelValue != null && (
              <p className="mt-2 max-w-[72ch] text-[0.875rem] leading-[1.45] text-ink-soft">
                Enterprise value, equity value, the intrinsic value and the projection are model outputs,
                and model vs price is computed from the price and the model value: none has a filing
                reference of its own.
              </p>
            )}
          </section>
        )}
        </>
        )}

        <footer className="fineprint mt-8 flex flex-wrap justify-between gap-x-6 gap-y-1 border-t border-ink pt-2">
          <p className="min-w-0 flex-1 basis-64">
            FinSight · equity research from SEC filings ·{" "}
            <a
              href="https://github.com/ROROK20/FinSight"
              target="_blank"
              rel="noopener noreferrer"
              className="link"
            >
              GitHub
            </a>{" "}
            ·{" "}
            <a
              href="https://github.com/ROROK20/FinSight/blob/main/LICENSE"
              target="_blank"
              rel="noopener noreferrer"
              className="link"
            >
              MIT License
            </a>{" "}
            ·{" "}
            {badge.footer}
          </p>
          <p>v0.1.0</p>
        </footer>
      </div>
    </main>
  );
}

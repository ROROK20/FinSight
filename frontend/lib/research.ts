/**
 * What the dashboard shows for a ticker: the engine's responses, merged, and
 * nothing else. There is no in-browser baseline.
 *
 * Those responses come from a running engine, or, with none, from the recording
 * of one in `recordedDemo.json` (scripts/record_demo.py; a Python test fails on
 * any drift from the engine). Both go through `mergeEngineData`, so a figure on
 * the page is always one the engine computed.
 */
import type {
  DcfResponse,
  KpisResponse,
  MetaResponse,
  SentimentResponse,
} from "./api";

/**
 * A figure the engine supplied, or null when there is none: the engine reported
 * none, or the call behind it failed. The page says which. It is never backfilled.
 */
export type Figure = number | null;

/** One projected year of the engine's DCF. A model output, not a forecast or a filing. */
export interface ProjectionPoint {
  period: string;
  revenue: number;
  fcf: number;
}

export interface CompanyResearch {
  ticker: string;
  companyName: string;
  sector: string | null;
  currentPrice: Figure;
  /**
   * The DCF model value per share. Not a price target: a mechanical output of
   * one fiscal year's figures at the engine's (default) assumptions.
   */
  modelValue: Figure;
  /** (model value − price) / price. Not an expected return. Null unless the engine gave both. */
  modelVsPrice: Figure;

  revenueTTM: Figure;
  revenueGrowth: Figure;
  grossMargin: Figure;
  operatingMargin: Figure;
  netMargin: Figure;
  fcf: Figure;
  eps: Figure;

  dcfIntrinsicValue: Figure;
  dcfEV: Figure;
  /** Enterprise value − net debt = equity value; ÷ shares = the value per share. */
  dcfNetDebt: Figure;
  dcfEquity: Figure;
  dcfShares: Figure;
  wacc: Figure;
  terminalGrowth: Figure;
  /** The DCF's explicit years, from `/dcf`. Empty without a DCF. */
  projection: ProjectionPoint[];

  sentimentLabel: "Bullish" | "Bearish" | "Neutral" | null;
  sentimentScore: Figure;
  guidanceTone: GuidanceTone | null;
  riskMentions: Figure;

  investmentThesis: string;

  fiscalYear?: string;
  /** e.g. "FY2026 10-K · FYE 2026-09-26" */
  dcfBasis?: string;
  /** DCF inputs that were model defaults rather than reported figures. */
  dcfDefaults?: string[];
  /** Company figures the filing did not report and the DCF assumed. */
  dcfAssumed?: string[];
  /** DCF inputs moved away from their reported figure, with what was done. */
  dcfAdjustments?: Record<string, string>;
  /** What the DCF's inputs cannot show, by field, in the engine's words. */
  dcfLimitations?: Record<string, string>;
  sentimentSynthetic?: boolean;
  /** The engine's reason there is no sentiment at all (live transcripts are off). */
  sentimentUnavailable?: string;
  /** Date of a bundled (non-live) price, e.g. "2026-10-30". */
  priceDate?: string;
  /** The DCF's valuation date (base fiscal year-end), e.g. "2026-09-26". */
  valuationDate?: string;
  /** Why model vs price is not shown although both figures exist (a split). */
  gapWithheld?: string;
  /** The engine's own reason for refusing a DCF (HTTP 422/404): no figure is shown. */
  dcfRefusal?: string;
}

/**
 * The engine's guidance-tone classes (finsight GUIDANCE_TONES): only an
 * explicit action on guidance counts; "not_stated" when the call states none.
 */
export const GUIDANCE_TONES = [
  "raised", "lowered", "mixed", "maintained", "initiated", "withdrawn", "not_stated",
] as const;
export type GuidanceTone = (typeof GUIDANCE_TONES)[number];

/** A ticker with no data: every figure null. Never a made-up one. */
export function emptyResearch(ticker: string): CompanyResearch {
  const upper = ticker.toUpperCase();
  return {
    ticker: upper,
    companyName: upper,
    sector: null,
    currentPrice: null,
    modelValue: null,
    modelVsPrice: null,
    revenueTTM: null,
    revenueGrowth: null,
    grossMargin: null,
    operatingMargin: null,
    netMargin: null,
    fcf: null,
    eps: null,
    dcfIntrinsicValue: null,
    dcfEV: null,
    dcfNetDebt: null,
    dcfEquity: null,
    dcfShares: null,
    wacc: null,
    terminalGrowth: null,
    projection: [],
    sentimentLabel: null,
    sentimentScore: null,
    guidanceTone: null,
    riskMentions: null,
    investmentThesis: "",
  };
}

/**
 * The summary sentence, templated from the engine's figures.
 *
 * It states figures, not a view: no "upside", no "fair value", and earnings-call tone only when
 * it came from a real transcript (`realSentiment`). A figure that is null is left
 * out or said to be not reported, and a failed call is never read as "not reported".
 */
export function buildThesis(d: {
  ticker: string;
  revenueGrowth: Figure;
  grossMargin: Figure;
  modelVsPrice: Figure;
  realSentiment: string | null;
  kpisUnavailable?: boolean;
}): string {
  const growth =
    d.revenueGrowth == null
      ? null
      : `revenue ${d.revenueGrowth >= 0 ? "grew" : "fell"} ${Math.abs(d.revenueGrowth * 100).toFixed(1)}%`;
  const margin = d.grossMargin == null ? null : `${(d.grossMargin * 100).toFixed(0)}% gross margin`;
  const figures = d.kpisUnavailable
    ? "financials unavailable: the engine call behind them failed."
    : growth && margin
      ? `${growth} with a ${margin}.`
      : growth
        ? `${growth}; gross margin not reported.`
        : margin
          ? `revenue growth not reported; a ${margin}.`
          : "revenue growth and gross margin not reported.";
  const gap =
    d.modelVsPrice == null
      ? " No model-vs-price figure: it needs both a reported price and a DCF model value."
      : ` The DCF model value is ${Math.abs(d.modelVsPrice * 100).toFixed(0)}% ${
          d.modelVsPrice >= 0 ? "above" : "below"
        } the price at the engine's default assumptions — a mechanical output, not a price target or advice.`;
  const tone = d.realSentiment
    ? ` The latest earnings call reads ${d.realSentiment.toLowerCase()} on the sentiment lexicon.`
    : "";
  return `${d.ticker}: ${figures}${gap}${tone}`;
}

/** Listed first, in this order: each can move the per-share value by itself. */
const VALUE_LIMITATIONS = ["current_debt", "long_term_debt_not_found", "debt_above_total", "share_count"];

/**
 * The limitations as the page lists them: the ones that can move the per-share value
 * first, then the rest by name, each with its field name in words.
 */
export function limitationRows(
  limitations: Record<string, string> | undefined,
): { key: string; label: string; text: string }[] {
  const rank = (key: string) => {
    const i = VALUE_LIMITATIONS.indexOf(key);
    return i < 0 ? VALUE_LIMITATIONS.length : i;
  };
  return Object.entries(limitations ?? {})
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([key, text]) => {
      const words = key.replace(/_/g, " ");
      return { key, label: words[0].toUpperCase() + words.slice(1), text };
    });
}

/** A limitation's text cut around its https links, so the page can make them links. */
export function linkParts(text: string): { text: string; href?: string }[] {
  return text
    .split(/(https:\/\/[^\s)]+)/)
    .filter(Boolean)
    .map((part) => (part.startsWith("https://") ? { text: part, href: part } : { text: part }));
}

/** The engine's answers for one ticker; a null part is a call that failed. */
export interface EngineParts {
  meta?: MetaResponse | null;
  kpis?: KpisResponse | null;
  dcf?: DcfResponse | null;
  sentiment?: SentimentResponse | null;
  /** The engine's reason for refusing a DCF (see api.dcf). */
  dcfRefusal?: string | null;
}

/**
 * Merge the engine's responses into what the page shows.
 *
 * Every figure of a part the engine delivered is the engine's, null included; a
 * part whose call failed leaves its figures null, and the page reads that panel
 * "Unavailable". The API serves no peer table, history, catalysts or risks, so
 * the page has none of them.
 */
export function mergeEngineData(ticker: string, parts: EngineParts): CompanyResearch {
  const out = emptyResearch(ticker);
  const { meta, kpis, dcf, sentiment, dcfRefusal } = parts;
  if (!meta && !kpis && !dcf && !sentiment && !dcfRefusal) return out;

  const num = (v: unknown): Figure => (typeof v === "number" && Number.isFinite(v) ? v : null);

  if (meta) {
    out.companyName = meta.company_name || out.ticker;
    out.sector = meta.sector || "Sector not reported";
    out.currentPrice =
      typeof meta.current_price === "number" && meta.current_price > 0 ? meta.current_price : null;
    out.priceDate = meta.price_date || undefined;
  }

  if (kpis) {
    out.revenueTTM = num(kpis.revenue);
    out.revenueGrowth = num(kpis.revenue_growth_yoy);
    out.grossMargin = num(kpis.gross_margin);
    out.operatingMargin = num(kpis.operating_margin);
    out.netMargin = num(kpis.net_margin);
    out.fcf = num(kpis.free_cash_flow);
    out.eps = num(kpis.eps_diluted);
    out.fiscalYear = kpis.period || undefined;
  }

  if (dcf) {
    out.dcfIntrinsicValue = num(dcf.intrinsic_value_per_share);
    out.dcfEV = num(dcf.enterprise_value_m);
    out.dcfNetDebt = num(dcf.assumptions?.net_debt_m);
    out.dcfEquity = num(dcf.equity_value_m);
    out.dcfShares = num(dcf.assumptions?.shares_outstanding_m);
    out.wacc = num(dcf.assumptions?.wacc);
    out.terminalGrowth = num(dcf.assumptions?.terminal_growth_rate);
    // The model value is the engine's DCF output; it is not a price target.
    out.modelValue = num(dcf.intrinsic_value_per_share);
    const p = dcf.provenance;
    if (p) out.dcfBasis = `${dcf.fiscal_year} ${p.form} · FYE ${p.fiscal_year_end}`;
    const vd = dcf.valuation_date ?? p?.fiscal_year_end;
    if (vd) out.valuationDate = vd;
    out.dcfDefaults = dcf.defaulted_inputs ?? [];
    out.dcfAssumed = dcf.assumed_fundamentals ?? [];
    out.dcfAdjustments = dcf.adjustments ?? {};
    out.dcfLimitations = dcf.limitations ?? {};
    const fcfs = dcf.projected_fcfs_m ?? [];
    out.projection = (dcf.projected_revenues_m ?? []).map((revenue, i) => ({
      period: `Year ${i + 1}`,
      revenue,
      fcf: fcfs[i],
    }));
  } else if (dcfRefusal) {
    // The engine answered, and declined: a statement, not a failed call. Its
    // reason shows and no number does.
    out.dcfRefusal = dcfRefusal;
  }

  if (sentiment && sentiment.results.length > 0) {
    const r = sentiment.results[0];
    out.sentimentScore = r.net_sentiment;
    out.sentimentLabel =
      r.overall_tone === "Bullish"
        ? "Bullish"
        : r.overall_tone === "Bearish"
          ? "Bearish"
          : "Neutral";
    // Every engine class is accepted; anything else reads "not stated".
    const gt = r.guidance_tone;
    out.guidanceTone = (GUIDANCE_TONES as readonly string[]).includes(gt)
      ? (gt as GuidanceTone)
      : "not_stated";
    out.riskMentions = r.risk_mentions;
    out.sentimentSynthetic = sentiment.synthetic === true;
  }

  if (sentiment?.available === false) out.sentimentUnavailable = sentiment.detail;

  // The gap only from an engine price AND an engine model value, and never
  // across a stock split: the share count is as its 10-K reported it, Yahoo's
  // price is split-adjusted, so a 10:1 split after the filing would read as ~−90%.
  const filed = dcf?.provenance?.per_share_filed;
  const split = (meta?.splits ?? []).filter((s) => filed && s.date > filed && s.ratio !== 1);
  // A live price with NO split history (Yahoo did not answer) is unknown, not
  // "no split": withheld like the report does. The demo bundles no split history, so
  // it runs no check.
  const splitsUnknown = meta?.mode === "live" && !Array.isArray(meta?.splits);
  out.gapWithheld = split.length
    ? `Not compared: a ${split.map((s) => `${s.ratio}:1 split on ${s.date}`).join(", ")} ` +
      `came after the share count's 10-K (filed ${filed}), so the per-share value and the ` +
      "split-adjusted price are on different share bases."
    : splitsUnknown
      ? "Not compared: the split history could not be retrieved, so a stock split since " +
        "the share count's 10-K cannot be ruled out."
      : undefined;
  out.modelVsPrice =
    meta && dcf && out.currentPrice != null && out.modelValue != null && !out.gapWithheld
      ? out.modelValue / out.currentPrice - 1
      : null;

  const realSentiment =
    sentiment && sentiment.results.length > 0 && sentiment.synthetic === false
      ? out.sentimentLabel
      : null;
  out.investmentThesis = buildThesis({
    ticker: out.ticker,
    revenueGrowth: out.revenueGrowth,
    grossMargin: out.grossMargin,
    modelVsPrice: out.modelVsPrice,
    realSentiment,
    kpisUnavailable: !kpis,
  });

  return out;
}

/** A recording of the engine's answers (`recordedDemo.json`). */
export interface Recording {
  captured: string;
  snapshot_retrieved: string;
  tickers: Record<
    string,
    { meta: MetaResponse; kpis: KpisResponse; dcf: DcfResponse; sentiment: SentimentResponse }
  >;
}

/**
 * The no-backend page for a ticker: the recording, merged exactly as a running
 * engine's answers are, or null when the recording does not hold it. Null means
 * the page shows an empty state and its reason: no ticker gets a made-up figure.
 */
export function recordedResearch(recording: Recording, ticker: string): CompanyResearch | null {
  const parts = recording.tickers[ticker.toUpperCase()];
  return parts ? mergeEngineData(ticker, parts) : null;
}

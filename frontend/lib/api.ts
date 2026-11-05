/**
 * Typed client for the FinSight backend (FastAPI).
 *
 * Reads NEXT_PUBLIC_API_URL (see `resolveApiUrl`) and calls the real engine
 * endpoints: /health, /meta, /kpis, /dcf, /sentiment.
 *
 * Design contract: this module NEVER throws on a network/HTTP error — every
 * call returns `null` on failure, and the page then reads that panel "Unavailable"
 * (or, when no call answers, replays `recordedDemo.json`). That keeps `next build` and a no-backend static
 * deploy fully functional. The page still shows what the engine said: `/dcf` REFUSING a
 * ticker (see `api.dcf`), and a server error stating its remedy (see `engineProblem`).
 */

/**
 * Where the engine is. NEXT_PUBLIC_API_URL is baked in at BUILD time. Without
 * it, only a development build (`next dev`) assumes the local engine on :8001;
 * a production build has no engine at all and replays the labelled recording,
 * making no request. It never falls back to localhost, which would have every
 * visitor's browser call its own machine.
 */
export function resolveApiUrl(env: { NEXT_PUBLIC_API_URL?: string; NODE_ENV?: string }): string | null {
  const configured = env.NEXT_PUBLIC_API_URL?.trim().replace(/\/$/, "");
  if (configured) return configured;
  return env.NODE_ENV === "development" ? "http://localhost:8001" : null;
}

export const API_URL = resolveApiUrl({
  NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  NODE_ENV: process.env.NODE_ENV,
});

/**
 * The ticker as a URL path segment, or null when it is not a ticker. Mirrors
 * the engine's own rule (1–10 of A–Z 0–9 . -, starting alphanumeric) and is
 * URL-encoded, so "AAPL/../TSLA" cannot fetch another ticker's figures.
 */
export function tickerPath(ticker: string): string | null {
  const t = ticker.trim().toUpperCase();
  return /^[A-Z0-9][A-Z0-9.-]{0,9}$/.test(t) && !t.includes("..") ? encodeURIComponent(t) : null;
}

// ---- Response shapes (mirror finsight/api/app.py) -------------------------

/** Every engine JSON response says where it came from. */
export type EngineMode = "live" | "demo";

/** Where a KPI / DCF figure came from (SEC XBRL, one fiscal year). */
export interface Provenance {
  source: string;
  form: string;
  fiscal_year: string;
  fiscal_year_end: string;
  /** The fiscal year's own 10-K (the earliest filing that reported it). */
  accession: string | null;
  filed: string | null;
  /** For each XBRL concept used: the 10-K its value was read from. */
  figure_filings?: Record<string, { accession: string | null; filed: string | null }>;
  /** Shares/EPS as that filing reported them — not split-adjusted. */
  per_share_basis?: string | null;
  /** The filing date of the share count (a split after it changes the basis). */
  per_share_filed?: string | null;
  /** What the figures cannot show, by field. */
  limitations?: Record<string, string>;
  concepts: Record<string, string>;
  missing: string[];
}

export interface MetaResponse {
  ticker: string;
  company_name: string;
  sector: string;
  current_price: number | null;
  mode: EngineMode;
  /** Demo only: the bundled close's date and source (not a live quote). */
  price_date?: string;
  price_source?: string;
  /** Live only: Yahoo's stock splits (its prices are split-adjusted); null = unknown. */
  splits?: { date: string; ratio: number }[] | null;
}

export interface KpisResponse {
  ticker: string;
  period: string;
  mode: EngineMode;
  revenue: number | null;
  revenue_growth_yoy: number | null;
  gross_profit: number | null;
  gross_margin: number | null;
  operating_income: number | null;
  operating_margin: number | null;
  net_income: number | null;
  net_margin: number | null;
  ebitda: number | null;
  eps_diluted: number | null;
  free_cash_flow: number | null;
  total_debt: number | null;
  cash_and_equivalents: number | null;
  capex: number | null;
  r_and_d_expense: number | null;
  shares_outstanding: number | null;
  extraction_method?: string;
  provenance?: Provenance;
  fallback_reason?: string;
}

export interface DcfResponse {
  ticker: string;
  mode: EngineMode;
  fiscal_year: string;
  /**
   * The date the DCF is struck at: the base 10-K's fiscal year-end (cash flows
   * discounted to it, net debt as of it). Not the price date.
   */
  valuation_date?: string | null;
  intrinsic_value_per_share: number;
  enterprise_value_m: number;
  equity_value_m: number;
  terminal_value_pct: number;
  pv_fcfs_m: number;
  projected_revenues_m: number[];
  projected_fcfs_m: number[];
  assumptions: {
    wacc: number;
    terminal_growth_rate: number;
    n_years: number;
    net_debt_m: number;
    shares_outstanding_m: number;
    capex_as_pct_revenue: number;
    terminal_capex_as_pct_revenue: number | null;
    /** CapEx / revenue used in each projection year. */
    capex_pct_schedule: number[];
  };
  assumption_sources: Record<string, string>;
  /** Inputs that were model defaults rather than reported figures. */
  defaulted_inputs: string[];
  /** The subset of defaulted_inputs that are company figures the filing did not report. */
  assumed_fundamentals?: string[];
  /** Every input the projection moved away from its reported figure, and how. */
  adjustments: Record<string, string>;
  /** What the inputs cannot show (e.g. depreciation-only D&A, finance leases). */
  limitations?: Record<string, string>;
  provenance: Provenance;
  fallback_reason?: string;
}

export interface SentimentResultItem {
  quarter: string;
  overall_tone: string;
  net_sentiment: number;
  /** One of finsight's GUIDANCE_TONES (see research.GUIDANCE_TONES). */
  guidance_tone: string;
  risk_mentions: number;
  /** Q&A minus prepared remarks; null when the call has no Q&A section. */
  sentiment_drift: number | null;
  /** True when computed from the synthetic placeholder transcript. */
  synthetic: boolean;
  source: string;
}

export interface SentimentResponse {
  ticker: string;
  mode: EngineMode;
  /** False when live transcripts are off on the server: no results, and `detail` says why. */
  available?: boolean;
  synthetic: boolean;
  results: SentimentResultItem[];
  detail?: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  demo_mode: boolean;
  demo_tickers: string[];
}

/**
 * The tickers an offline demo engine can answer for, or null when the engine
 * is live (any ticker) or did not answer /health. A demo engine refuses every
 * other ticker with a 404 instead of looking it up.
 */
export function demoScope(h: HealthResponse | null): string[] | null {
  return h && h.demo_mode === true && Array.isArray(h.demo_tickers) ? h.demo_tickers : null;
}

/**
 * A response that does not say whether it is live or demo cannot back an
 * honest badge, so it is treated as if the call had failed.
 */
export function tagged<T extends { mode?: unknown }>(r: T | null): T | null {
  return r && (r.mode === "live" || r.mode === "demo") ? r : null;
}

// ---- Internal fetch helper ------------------------------------------------

/** `/dcf`'s answer: a valuation, the engine's reason for refusing one, or neither (call failed). */
export interface DcfOutcome {
  dcf: DcfResponse | null;
  refusal: string | null;
}

/**
 * The engine's stated reason, for a status that means "I answered, and no":
 * 422 (it will not value this on these figures) or 404 (it has no data). A body
 * without a string `detail` is not the engine (a proxy's error page, say), so
 * it counts as a failed call.
 */
export async function refusalReason(res: {
  status: number;
  json: () => Promise<unknown>;
}): Promise<string | null> {
  if (res.status !== 422 && res.status !== 404) return null;
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === "string" && body.detail ? body.detail : null;
}

/**
 * What the engine said when it answered a data call with a server error: the 503 naming
 * the remedy for an unset SEC_EDGAR_USER_AGENT, say. A body without a string `detail` is
 * not the engine, so it says nothing.
 */
export async function engineProblem(res: {
  status: number;
  json: () => Promise<unknown>;
}): Promise<string | null> {
  if (res.status < 500) return null;
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === "string" && body.detail ? body.detail : null;
}

/** The opening clause of the notice on a page no engine call could fill. */
export function noDataReason(engineSaid: string | null, apiUrl: string | null = API_URL): string {
  if (engineSaid) return `The engine answered but could not serve this ("${engineSaid.trim()}")`;
  return apiUrl ? "The configured backend did not answer" : "This deployment has no backend";
}

async function getJson<T>(
  path: string | null,
  timeoutMs = 8000,
  onError?: (res: Response) => Promise<void>,
): Promise<T | null> {
  // No engine configured (production build without NEXT_PUBLIC_API_URL) or
  // not a valid ticker: no request at all.
  if (!API_URL || path === null) return null;
  // AbortController guards against a hung backend stalling the UI.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_URL}${path}`, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!res.ok) {
      await onError?.(res);
      return null;
    }
    return (await res.json()) as T;
  } catch {
    // Network down, CORS, timeout, JSON parse error — all map to "fall back".
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const seg = (prefix: string, ticker: string): string | null => {
  const t = tickerPath(ticker);
  return t === null ? null : prefix + t;
};

// ---- Public API -----------------------------------------------------------

/** Told of a non-2xx answer, whose body it may read. A call still resolves to null. */
export type OnFailedCall = (res: Response) => Promise<void>;

export const api = {
  health: () => getJson<HealthResponse>("/health", 3000),
  meta: (ticker: string, onFailed?: OnFailedCall) =>
    getJson<MetaResponse>(seg("/meta/", ticker), 8000, onFailed),
  kpis: (ticker: string, onFailed?: OnFailedCall) =>
    getJson<KpisResponse>(seg("/kpis/", ticker), 8000, onFailed),
  dcf: async (ticker: string, onFailed?: OnFailedCall): Promise<DcfOutcome> => {
    let refusal: string | null = null;
    const dcf = await getJson<DcfResponse>(seg("/dcf/", ticker), 8000, async (res) => {
      refusal = await refusalReason(res);
      if (res.status >= 500) await onFailed?.(res);
    });
    return { dcf, refusal };
  },
  sentiment: (ticker: string, onFailed?: OnFailedCall) =>
    getJson<SentimentResponse>(seg("/sentiment/", ticker), 8000, onFailed),
};

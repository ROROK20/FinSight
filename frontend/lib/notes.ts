/**
 * The note's numbered footnotes: where each figure came from, in the engine's own
 * words. A note exists only when the engine's response carries a source for that
 * figure and the figure is on the page, so nothing here cites what the response
 * does not hold. Enterprise value, equity value and the projection get no note:
 * they are model outputs, and the closing line of the notes says so.
 */
import type { Provenance } from "./api";
import { fmtMillions as m } from "./format";
import type { CompanyResearch } from "./research";

export type NoteKey =
  | "price" | "model" | "gap"
  | "revenue" | "growth" | "gross" | "op" | "net" | "fcf" | "eps"
  | "basis" | "netDebt" | "shares" | "wacc" | "terminal";

/** Reading order down the page. A note's number is its place among those that exist. */
const ORDER: readonly NoteKey[] = [
  "price", "model", "gap",
  "revenue", "growth", "gross", "op", "net", "fcf", "eps",
  "basis", "netDebt", "shares", "wacc", "terminal",
];

const LABEL: Record<NoteKey, string> = {
  price: "Price",
  model: "DCF model value",
  gap: "Model vs price",
  revenue: "Revenue",
  growth: "Revenue growth",
  gross: "Gross margin",
  op: "Op margin",
  net: "Net margin",
  fcf: "Reported FCF",
  eps: "EPS (diluted)",
  basis: "DCF basis",
  netDebt: "Net debt",
  shares: "Shares",
  wacc: "WACC",
  terminal: "Terminal growth",
};

export interface Note {
  key: NoteKey;
  number: number;
  label: string;
  text: string;
}

export interface NoteSheet {
  /** The filing the figures come from unless a note names another, or null when no response names one. */
  filing: string | null;
  notes: Note[];
  number: Partial<Record<NoteKey, number>>;
}

/** "Form 10-K for FY2026 (fiscal year ended 2026-09-26), accession …, filed …", from what the response holds. */
export function filingCitation(p: Provenance | undefined): string | null {
  if (!p?.form) return null;
  const year = p.fiscal_year ? ` for ${p.fiscal_year}` : "";
  const end = p.fiscal_year_end ? ` (fiscal year ended ${p.fiscal_year_end})` : "";
  const parts = [`Form ${p.form}${year}${end}`];
  if (p.accession) parts.push(`accession ${p.accession}`);
  if (p.filed) parts.push(`filed ${p.filed}`);
  return parts.join(", ");
}

/** The XBRL concepts behind a figure, and any filing they were read from other than the note's lead one. */
function concepts(p: Provenance, names: (string | undefined)[], separator = " ÷ "): string | null {
  if (names.some((n) => !n)) return null;
  const read = names as string[];
  const elsewhere = new Map<string, string | null>();
  for (const name of read) {
    for (const tag of name.match(/[A-Z][A-Za-z0-9]+/g) ?? []) {
      const filing = p.figure_filings?.[tag];
      if (filing?.accession && filing.accession !== p.accession) {
        elsewhere.set(filing.accession, filing.filed);
      }
    }
  }
  const others = Array.from(elsewhere, ([accession, filed]) =>
    `read from ${accession}${filed ? `, filed ${filed}` : ""}`,
  );
  const tags = read.join(separator);
  return others.length ? `${tags} (${others.join("; ")})` : tags;
}

const sentence = (s: string) => {
  const t = s.trim();
  return `${t[0].toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? "" : "."}`;
};

/** The engine's "default: 10.0%" as a reader takes it: a model input, not a filed figure. */
const assumption = (s: string) => {
  const d = /^default:\s*(.+)$/.exec(s.trim());
  return d ? `Model default, ${d[1]}: no filing reports it.` : sentence(s);
};

const words = (key: string) => key.replace(/_/g, " ");

export function buildNotes(d: CompanyResearch): NoteSheet {
  const { priceSource, kpiProvenance: kpi, dcfProvenance: dcf, assumptionSources } = d.sources ?? {};
  const text: Partial<Record<NoteKey, string>> = {};

  if (priceSource && d.currentPrice != null) {
    text.price = d.priceDate
      ? `${priceSource}, ${d.priceDate}. Not a live quote.`
      : sentence(priceSource);
  }

  const dcfFiling = filingCitation(dcf);
  if (d.modelValue != null && dcfFiling) {
    text.model = `A model output: no filing reports it. Its reported inputs come from ${dcfFiling}; the rest are the engine's assumptions.`;
  }
  if (d.modelVsPrice != null && d.valuationDate) {
    text.gap =
      "The DCF is struck at the base fiscal year-end (cash flows discounted to it, net debt as of it) and is not rolled forward, so the gap compares values on two different dates.";
  }

  if (kpi) {
    const c = kpi.concepts ?? {};
    const put = (key: NoteKey, figure: number | null, tags: string | null, after = "") => {
      if (figure != null && tags) text[key] = `${tags}.${after ? ` ${after}` : ""}`;
    };
    put("revenue", d.revenueTTM, concepts(kpi, [c.revenue]));
    put("growth", d.revenueGrowth, concepts(kpi, [c.revenue_growth_yoy]));
    put("gross", d.grossMargin, concepts(kpi, [c.gross_profit, c.revenue]));
    put("op", d.operatingMargin, concepts(kpi, [c.operating_income, c.revenue]));
    put("net", d.netMargin, concepts(kpi, [c.net_income, c.revenue]));
    put(
      "fcf",
      d.fcf,
      concepts(kpi, [c.free_cash_flow]),
      "Operating cash flow − CapEx, from the 10-K. Not the DCF's projected FCF, which starts from operating income and so keeps stock comp as a cost.",
    );
    put("eps", d.eps, concepts(kpi, [c.eps_diluted]), kpi.per_share_basis ? sentence(kpi.per_share_basis) : "");
  }

  const defaults = d.dcfDefaults ?? [];
  const adjustments = Object.entries(d.dcfAdjustments ?? {});
  if (defaults.length || adjustments.length) {
    const parts: string[] = [];
    if (defaults.length) parts.push(`Model defaults, not reported figures: ${defaults.map(words).join(", ")}.`);
    if (adjustments.length) {
      parts.push(
        `Reported figures the projection adjusts: ${adjustments.map(([k, v]) => `${words(k)}, ${v}`).join("; ")}.`,
      );
    }
    text.basis = parts.join(" ");
  }

  if (assumptionSources) {
    const { net_debt: netDebt, shares_outstanding: shares, wacc, terminal_growth_rate: terminal } = assumptionSources;
    if (netDebt && d.dcfNetDebt != null) {
      const debt = d.reported?.totalDebt;
      const cash = d.reported?.cash;
      const ties = debt != null && cash != null && Math.abs(debt - cash - d.dcfNetDebt) < 0.5;
      const worked = ties ? `${m(debt)} total debt − ${m(cash)} cash = ${m(d.dcfNetDebt)} ($M). ` : "";
      text.netDebt = `${worked}${sentence(netDebt)} Enterprise value − net debt = equity value; equity value ÷ shares = the value per share.`;
    }
    if (shares && d.dcfShares != null) text.shares = sentence(shares);
    if (wacc && d.wacc != null) text.wacc = assumption(wacc);
    if (terminal && d.terminalGrowth != null) text.terminal = assumption(terminal);
  }

  const number: NoteSheet["number"] = {};
  const notes: Note[] = [];
  for (const key of ORDER) {
    const body = text[key];
    if (!body) continue;
    notes.push({ key, number: notes.length + 1, label: LABEL[key], text: body });
    number[key] = notes.length;
  }

  const lead = filingCitation(kpi) ?? dcfFiling;
  const source = (kpi ?? dcf)?.source;
  return {
    filing: lead ? `${lead}${source ? `, read from the ${source}` : ""}` : null,
    notes,
    number,
  };
}

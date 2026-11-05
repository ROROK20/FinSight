/**
 * mergeEngineData must show the engine's figures and nothing else: a figure the engine does
 * not report stays absent, and there is no in-browser baseline to fill it from.
 *
 *   cd frontend && npm test
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadTs } from "./load-ts.mjs";

const { mergeEngineData, emptyResearch, recordedResearch, limitationRows, linkParts } =
  await loadTs("lib/research.ts");
const RECORDING = JSON.parse(readFileSync(new URL("../lib/recordedDemo.json", import.meta.url), "utf8"));

const JPM_KPIS = {
  ticker: "JPM", mode: "live", period: "FY2025", revenue: 180000, revenue_growth_yoy: null,
  gross_margin: null, operating_margin: null, net_margin: 0.3, free_cash_flow: null,
  eps_diluted: 19.5,
};
const JPM_META = {
  ticker: "JPM", mode: "live", company_name: "JPMorgan Chase", sector: "Financials",
  current_price: null,
};
const DCF = {
  ticker: "AAPL", mode: "demo", fiscal_year: "FY2025", intrinsic_value_per_share: 106.7053,
  enterprise_value_m: 1663800, assumptions: { wacc: 0.1, terminal_growth_rate: 0.03 },
  provenance: { form: "10-K", fiscal_year_end: "2025-09-27" }, defaulted_inputs: [],
  adjustments: {}, valuation_date: "2025-09-27",
};

test("a null from the engine is not reported, never a sample value", () => {
  const out = mergeEngineData("JPM", { meta: JPM_META, kpis: JPM_KPIS });
  assert.equal(out.currentPrice, null);
  assert.equal(out.revenueGrowth, null);
  assert.equal(out.grossMargin, null);
  assert.equal(out.operatingMargin, null);
  assert.equal(out.fcf, null);
  // Reported figures are the engine's.
  assert.equal(out.revenueTTM, 180000);
  assert.equal(out.netMargin, 0.3);
  assert.equal(out.eps, 19.5);
  assert.equal(out.companyName, "JPMorgan Chase");
});

test("model vs price needs an engine price AND an engine model value", () => {
  assert.equal(mergeEngineData("JPM", { meta: JPM_META, kpis: JPM_KPIS }).modelVsPrice, null);
  // Real DCF, failed /meta (no price): no gap.
  assert.equal(mergeEngineData("AAPL", { dcf: DCF }).modelVsPrice, null);
  // Both from the engine: the gap is computed.
  const meta = { ticker: "AAPL", mode: "demo", company_name: "Apple Inc.", sector: "Technology",
                 current_price: 344.63, price_date: "2026-10-30" };
  const both = mergeEngineData("AAPL", { meta, dcf: DCF });
  assert.ok(Math.abs(both.modelVsPrice - (106.7053 / 344.63 - 1)) < 1e-12);
});

test("the summary quotes only engine figures", () => {
  const out = mergeEngineData("JPM", { meta: JPM_META, kpis: JPM_KPIS });
  assert.equal(
    out.investmentThesis,
    "JPM: revenue growth and gross margin not reported. No model-vs-price figure: it needs " +
      "both a reported price and a DCF model value.",
  );
});

test("a failed /kpis reads unavailable, not 'not reported'", () => {
  assert.match(mergeEngineData("JPM", { meta: JPM_META }).investmentThesis,
               /^JPM: financials unavailable: the engine call behind them failed\./);
});

test("the demo summary is unchanged when every figure is reported", () => {
  const meta = { ticker: "AAPL", mode: "demo", company_name: "Apple Inc.", sector: "Technology",
                 current_price: 344.63, price_date: "2026-10-30" };
  const kpis = { ticker: "AAPL", mode: "demo", period: "FY2025", revenue: 416161,
                 revenue_growth_yoy: 0.0643, gross_margin: 0.4691, operating_margin: 0.3197,
                 net_margin: 0.2692, free_cash_flow: 98767, eps_diluted: 7.46 };
  assert.equal(
    mergeEngineData("AAPL", { meta, kpis, dcf: DCF }).investmentThesis,
    "AAPL: revenue grew 6.4% with a 47% gross margin. The DCF model value is 69% below the " +
      "price at the engine's default assumptions — a mechanical output, not a price target or advice.",
  );
});

test("with no engine data at all, every figure is null", () => {
  for (const t of ["AAPL", "ZZZZ"]) {
    assert.deepEqual(mergeEngineData(t, {}), emptyResearch(t));
    const figures = Object.entries(emptyResearch(t)).filter(([, v]) => typeof v === "number");
    assert.deepEqual(figures, [], t);
  }
});

test("company figures the DCF assumed are carried to the panel", () => {
  const out = mergeEngineData("X", { dcf: { ...DCF, assumed_fundamentals: ["net_debt"] } });
  assert.deepEqual(out.dcfAssumed, ["net_debt"]);
  assert.deepEqual(mergeEngineData("AAPL", { dcf: DCF }).dcfAssumed, []);
});

test("model vs price is withheld across a stock split", () => {
  // Live: Yahoo's price is split-adjusted, the 10-K share count is not; a
  // 10:1 split after the filing would read as a ~-90% gap.
  const meta = { ticker: "NVDA", mode: "live", company_name: "NVIDIA", sector: "Tech",
                 current_price: 120.0, splits: [{ date: "2024-06-10", ratio: 10 }] };
  const dcf = { ...DCF, intrinsic_value_per_share: 940.0,
                provenance: { ...DCF.provenance, per_share_filed: "2024-02-21" } };
  const out = mergeEngineData("NVDA", { meta, dcf });
  assert.equal(out.modelVsPrice, null);
  assert.match(out.gapWithheld, /10:1 split on 2024-06-10 came after the share count's 10-K \(filed 2024-02-21\)/);
  // A split BEFORE the filing changes nothing.
  const before = mergeEngineData("NVDA", { meta: { ...meta, splits: [{ date: "2021-07-20", ratio: 4 }] }, dcf });
  assert.equal(before.gapWithheld, undefined);
  assert.ok(Math.abs(before.modelVsPrice - (940 / 120 - 1)) < 1e-12);
});

test("an unknown split history withholds the live gap; the demo needs none", () => {
  const live = { ticker: "AAPL", mode: "live", company_name: "Apple Inc.", sector: "Technology",
                 current_price: 250.0, splits: null };
  const out = mergeEngineData("AAPL", { meta: live, dcf: DCF });
  assert.equal(out.modelVsPrice, null);
  assert.match(out.gapWithheld, /split history could not be retrieved/);
  const demo = { ticker: "AAPL", mode: "demo", company_name: "Apple Inc.", sector: "Technology",
                 current_price: 344.63, price_date: "2026-10-30" };
  assert.equal(mergeEngineData("AAPL", { meta: demo, dcf: DCF }).gapWithheld, undefined);
});

// ---- The recording: what a no-backend deploy shows ---------------

const DEMO = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"];

test("the recording holds the five demo tickers, dated", () => {
  assert.deepEqual(Object.keys(RECORDING.tickers), DEMO);
  assert.match(RECORDING.captured, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(RECORDING.snapshot_retrieved, "2026-11-04");
});

test("offline, a demo ticker shows the engine's figures, not invented ones", () => {
  const aapl = recordedResearch(RECORDING, "AAPL");
  assert.equal(aapl.modelValue.toFixed(2), "165.31");
  assert.equal(aapl.dcfEV.toFixed(1), "2477221.0");
  assert.equal(aapl.revenueTTM, 478834);
  assert.equal(aapl.fiscalYear, "FY2026");
  assert.equal(aapl.currentPrice, 344.63);
  assert.equal(aapl.priceDate, "2026-10-30");
  assert.equal(aapl.modelVsPrice.toFixed(3), "-0.520");
  const tsla = recordedResearch(RECORDING, "TSLA");
  assert.equal(tsla.modelValue.toFixed(2), "13.19");
  assert.equal(tsla.revenueTTM, 94827);
  for (const t of DEMO) {
    const r = recordedResearch(RECORDING, t);
    for (const invented of [220, 475, 145, 195, 3126720]) assert.notEqual(r.modelValue, invented, t);
    assert.equal(r.projection.length, 5, t);
    assert.deepEqual(r, mergeEngineData(t, RECORDING.tickers[t]), t);
  }
});

test("the recorded sentiment panel stays labelled synthetic", () => {
  for (const t of DEMO) {
    const r = recordedResearch(RECORDING, t);
    assert.equal(r.sentimentSynthetic, true, t);
    assert.equal(r.guidanceTone, "not_stated", t);
    assert.doesNotMatch(r.investmentThesis, /earnings call/, t);
  }
});

test("offline, any other ticker has no research at all", () => {
  // getResearch("AMZN") read "9% above the price" from a hash of its name.
  for (const t of ["AMZN", "JPM", "ZZZZ", "aapl/../tsla", ""])
    assert.equal(recordedResearch(RECORDING, t), null, t);
  assert.notEqual(recordedResearch(RECORDING, "aapl"), null);
});

test("a recording is not read as a live answer", () => {
  for (const t of DEMO) {
    const { meta, kpis, dcf, sentiment } = RECORDING.tickers[t];
    for (const part of [meta, kpis, dcf, sentiment]) assert.equal(part.mode, "demo", t);
  }
});

test("the projection is the engine's, year by year", () => {
  const { dcf } = RECORDING.tickers.AAPL;
  const r = recordedResearch(RECORDING, "AAPL");
  assert.deepEqual(r.projection.map((p) => p.revenue), dcf.projected_revenues_m);
  assert.deepEqual(r.projection.map((p) => p.fcf), dcf.projected_fcfs_m);
  assert.deepEqual(r.projection.map((p) => p.period), ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5"]);
});

// ---- A refused DCF: the engine's reason and no number -----------

const REFUSAL = "Cannot value JPM: revenue or diluted shares are not reported.";

test("a refused DCF shows the engine's reason and no number", () => {
  // mergeEngineData("JPM", { dcf: null, … }) read $290.90 with an EV of $4.3T.
  const out = mergeEngineData("JPM", { meta: JPM_META, kpis: JPM_KPIS, dcf: null, sentiment: null,
                                     dcfRefusal: REFUSAL });
  assert.equal(out.dcfRefusal, REFUSAL);
  for (const f of ["modelValue", "dcfIntrinsicValue", "dcfEV", "wacc", "terminalGrowth", "modelVsPrice"])
    assert.equal(out[f], null, f);
  assert.equal(out.dcfBasis, undefined);
  assert.doesNotMatch(out.investmentThesis, /\$|above|below/);
});

test("a refusal alone is still the engine's answer", () => {
  const out = mergeEngineData("JPM", { dcfRefusal: REFUSAL });
  assert.equal(out.modelValue, null);
  assert.equal(out.dcfRefusal, REFUSAL);
});

test("a failed /dcf leaves its figures null, and a real DCF ignores a refusal", () => {
  const failed = mergeEngineData("JPM", { meta: JPM_META, kpis: JPM_KPIS, dcf: null, dcfRefusal: null });
  assert.equal(failed.dcfRefusal, undefined);
  for (const f of ["modelValue", "dcfIntrinsicValue", "dcfEV", "wacc", "terminalGrowth", "modelVsPrice"])
    assert.equal(failed[f], null, f);
  assert.deepEqual(failed.projection, []);
  const real = mergeEngineData("AAPL", { dcf: DCF, dcfRefusal: REFUSAL });
  assert.equal(real.dcfRefusal, undefined);
  assert.equal(real.modelValue, 106.7053);
});

test("a failed /meta leaves the price null", () => {
  const out = mergeEngineData("AAPL", { dcf: DCF });
  assert.equal(out.currentPrice, null);
  assert.equal(out.sector, null);
  assert.equal(out.modelVsPrice, null);
});

test("a failed /sentiment leaves the panel null", () => {
  const out = mergeEngineData("AAPL", { dcf: DCF });
  for (const f of ["sentimentLabel", "sentimentScore", "guidanceTone", "riskMentions"])
    assert.equal(out[f], null, f);
});

test("the DCF's limitations reach the page, current debt first", () => {
  const out = mergeEngineData("AAPL", {
    dcf: { ...DCF, limitations: { finance_leases: "b", current_debt: "a", steady_state_capex: "c" } },
  });
  assert.deepEqual(
    limitationRows(out.dcfLimitations).map((r) => [r.key, r.label, r.text]),
    [["current_debt", "Current debt", "a"], ["finance_leases", "Finance leases", "b"],
     ["steady_state_capex", "Steady state capex", "c"]],
  );
  assert.deepEqual(limitationRows(undefined), []);
});

test("the limitations that move the per-share value lead the list", () => {
  const keys = limitationRows({
    finance_leases: "a", share_count: "b", current_debt: "c", depreciation_amortization: "d",
  }).map((r) => r.key);
  assert.deepEqual(keys, ["current_debt", "share_count", "depreciation_amortization", "finance_leases"]);
});

test("a filing link in a limitation comes out as a link, and only https", () => {
  assert.deepEqual(
    linkParts("its 10-K (https://www.sec.gov/a/b.htm) shows http://x"),
    [{ text: "its 10-K (" }, { text: "https://www.sec.gov/a/b.htm", href: "https://www.sec.gov/a/b.htm" },
     { text: ") shows http://x" }],
  );
});

test("the recording's Tesla page carries the current-debt disclosure", () => {
  const tsla = recordedResearch(RECORDING, "TSLA");
  const note = tsla.dcfLimitations.current_debt;
  assert.match(note, /The demo's debt excludes current debt/);
  assert.match(note, /\$1,569M/);
  assert.match(note, /https:\/\/www\.sec\.gov\/Archives\/edgar\/data\/1318605\/000162828026003952\/tsla-20251231\.htm/);
  assert.match(note, /about 3% of the \$13\.19 value/);
  assert.equal(recordedResearch(RECORDING, "AAPL").dcfLimitations.current_debt, undefined);
});

test("net debt, equity value and shares reach the page, so EV reconciles to the value", () => {
  for (const [ticker, parts] of Object.entries(RECORDING.tickers)) {
    const out = mergeEngineData(ticker, parts);
    assert.ok(Math.abs(out.dcfEV - out.dcfNetDebt - out.dcfEquity) < 1e-3, ticker);
    assert.ok(Math.abs(out.dcfEquity / out.dcfShares - out.modelValue) < 1e-6, ticker);
    assert.match(out.dcfLimitations.marketable_securities, /Marketable securities/, ticker);
  }
  assert.equal(mergeEngineData("AAPL", { meta: { mode: "demo" } }).dcfNetDebt, null);
});

test("sentiment that is off reads as not available, with the engine's reason and no score", () => {
  const out = mergeEngineData("JPM", {
    kpis: JPM_KPIS,
    sentiment: { ticker: "JPM", mode: "live", available: false, synthetic: false, results: [],
                 detail: "Not available: live earnings-call transcripts are off." },
  });
  assert.equal(out.sentimentUnavailable, "Not available: live earnings-call transcripts are off.");
  assert.equal(out.sentimentLabel, null);
  assert.equal(out.sentimentScore, null);
  assert.equal(out.guidanceTone, null);
  assert.equal(recordedResearch(RECORDING, "AAPL").sentimentUnavailable, undefined);
});

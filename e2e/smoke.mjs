/**
 * FinSight scripted live-fire smoke driver: the end-to-end release check.
 *
 * Proves in ONE free, repeatable command that FinSight's offline demo mode —
 * its signature behaviour (every demo response self-identifies, and the
 * bundled SEC data runs through the REAL extraction + DCF engine) — reproduces over HTTP
 * AND in a real browser against the real Python engine, with zero console
 * errors.
 *
 * The demo signature, re-derived 2026-11-04 (deterministic: FINSIGHT_DEMO=1
 * runs the live extraction + DCFModel over the bundled SEC companyfacts
 * snapshot retrieved 2026-11-04 — no network, no keys, no randomness):
 *   - GET /dcf/AAPL is tagged `"mode":"demo"` (the honest-badge product
 *     feature — the demo never masquerades as live);
 *   - the DCF intrinsic value per share is the deterministic $165.31
 *     (165.3101) for Apple's FY2026 10-K, computed by the real engine (WACC
 *     0.10, terminal growth 0.03, 5-year FCF projection, ΔNWC on the revenue
 *     change, CapEx fading from the reported 1.9% of revenue to the 1.8% steady
 *     state) and re-derived independently in tests/hand_derivation.py;
 *   - the dashboard renders that $165.31 under "Intrinsic Value" and flies the
 *     honest "Demo data" badge (NOT "Live data");
 *   - the sentiment panel is tagged "Synthetic" (demo sentiment is computed
 *     from the placeholder transcript) and the DCF panel states its basis,
 *     "FY2026 10-K · FYE 2026-09-26", with "4 model defaults" (WACC, terminal
 *     growth, NWC, tax) and "2 adjustments" (the growth and CapEx fades);
 *   - the hero card dates both sides of "Model vs price": valuation date
 *     2026-09-26 (the base fiscal year-end) and price close 2026-10-30;
 *   - negative control: with /dcf aborted in the browser,
 *     the badge reads "Partial data", never "Demo data"/"Live data", and the
 *     DCF panel reads Unavailable with no figure;
 *   - a ticker outside the demo set (AMZN) reads "Not in
 *     demo set" with an explanatory banner, the engine is never asked about it
 *     (demo mode is offline for every ticker), and no figure shows;
 *   - a /dcf the engine refuses shows the engine's reason and no number;
 *   - no engine at all: the page replays the recording, whose
 *     badge says so; AAPL reads the engine's $165.31, and AMZN an empty state
 *     with its reason and no figure;
 *   - no panel is invented in any state;
 *   - the recording's Tesla page says its debt excludes current debt, and the DCF panel
 *     prints net debt, equity value, shares and the inputs' limitations;
 *   - an engine that answers /health but 503s every data call: the page replays the
 *     recording and shows the engine's stated remedy.
 *
 * It self-provisions: spawns the FastAPI engine with FINSIGHT_DEMO=1 (:8001)
 * and the Next.js UI (:3002), waits for readiness, asserts, and tears BOTH
 * down in a `finally` (also on failure). Nothing is left running.
 *
 *   node e2e/smoke.mjs        # from the repo root; exit 0 = all pass, 1 = any fail
 *
 * It drives the Playwright *library* from a plain .mjs — NOT @playwright/test
 * (its TS spec loader breaks under Node 23). playwright is a devDependency of
 * frontend/, resolved from there though this file lives at the repo root. The
 * "Demo data" badge is matched with { exact: true } and waited-for so the
 * brief loading→settled flash on cold start can't cause a false pass. The API
 * MUST run with FINSIGHT_DEMO=1 or it hits live SEC EDGAR. Assertions target
 * the LIVE JSON shape, probed 2026-07-08.
 */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { setTimeout as sleep } from "node:timers/promises";
import { connect } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
const FRONTEND = join(ROOT, "frontend");
const require = createRequire(join(FRONTEND, "package.json"));
const { chromium } = require("playwright");

const API_PORT = 8001;
const UI_PORT = 3002;
const API = `http://127.0.0.1:${API_PORT}`;
const UI = `http://127.0.0.1:${UI_PORT}`;

// Demo signature values (deterministic; FINSIGHT_DEMO=1 snapshot; re-derived 2026-11-04).
const DEMO_TICKER = "AAPL";
const EXPECT_INTRINSIC = 165.3101; // /dcf/AAPL intrinsic_value_per_share
const INTRINSIC_TOL = 0.01; // pure deterministic fixture math; tolerance is rounding slack only
const EXPECT_INTRINSIC_DISPLAY = "$165.31"; // page.tsx renders `$${value.toFixed(2)}`

// ---- pass/fail bookkeeping ---------------------------------------------------
let pass = 0,
  fail = 0;
const ok = (n) => {
  pass++;
  console.log(`  ✓ ${n}`);
};
const bad = (n, e) => {
  fail++;
  console.log(`  ✗ ${n}${e ? " — " + e : ""}`);
};
async function check(name, fn) {
  try {
    await fn();
    ok(name);
  } catch (e) {
    bad(name, (e && e.message ? e.message : String(e)).split("\n")[0].slice(0, 180));
  }
}

// ---- server lifecycle -----------------------------------------------------
const children = [];
function launch(label, cmd, args, cwd, env) {
  // detached:true -> each server leads its own process group so teardown can
  // SIGKILL the whole tree (uvicorn, `next dev` compiler children).
  const child = spawn(cmd, args, {
    cwd,
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ...env },
  });
  child.label = label;
  child.errtail = [];
  const keep = (d) => {
    for (const line of String(d).split("\n")) {
      if (line.trim()) {
        child.errtail.push(line);
        if (child.errtail.length > 30) child.errtail.shift();
      }
    }
  };
  child.stderr.on("data", keep);
  child.stdout.on("data", keep);
  child.on("error", (e) => keep(`spawn error: ${e.message}`));
  children.push(child);
  return child;
}
function killAll() {
  for (const c of children) {
    if (c.pid == null || c.exitCode !== null) continue;
    try {
      process.kill(-c.pid, "SIGKILL");
    } catch {
      try {
        c.kill("SIGKILL");
      } catch {}
    }
  }
}
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    killAll();
    process.exit(1);
  });
}

function portListening(port) {
  return new Promise((resolve) => {
    const s = connect({ host: "127.0.0.1", port }, () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
    s.setTimeout(1000, () => {
      s.destroy();
      resolve(false);
    });
  });
}
async function waitUntil(what, fn, timeoutMs, child) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (child && child.exitCode !== null) {
      throw new Error(`${child.label} exited early (code ${child.exitCode}): ${child.errtail.slice(-6).join(" | ")}`);
    }
    try {
      if (await fn()) return;
    } catch {}
    await sleep(400);
  }
  throw new Error(`timeout waiting for ${what} after ${timeoutMs}ms${child ? ": " + child.errtail.slice(-6).join(" | ") : ""}`);
}

const near = (a, b, tol) => typeof a === "number" && Math.abs(a - b) <= tol;

let browser;
const t0 = Date.now();
try {
  // Preflight: refuse to run against a stale server on our ports (8001/3002
  // are finsight's own) — that would be a false pass.
  for (const [port, name] of [[API_PORT, "API"], [UI_PORT, "frontend"]]) {
    if (await portListening(port)) {
      throw new Error(`port ${port} (${name}) already in use — stop the stale process and re-run`);
    }
  }

  console.log("Starting servers:");
  // FINSIGHT_DEMO=1 is load-bearing — without it the engine hits live SEC
  // EDGAR (network, rate limits, non-deterministic). The signature IS the
  // offline demo path.
  const apiProc = launch(
    "api",
    "python3",
    ["-m", "uvicorn", "finsight.api.app:app", "--host", "127.0.0.1", "--port", String(API_PORT)],
    ROOT,
    { FINSIGHT_DEMO: "1" },
  );
  await check("API up in demo mode (/health demo_mode=true)", () =>
    waitUntil(
      "API /health",
      async () => {
        const r = await fetch(`${API}/health`, { signal: AbortSignal.timeout(2000) });
        if (!r.ok) return false;
        const j = await r.json();
        if (j.status !== "ok") return false;
        if (j.demo_mode !== true) throw new Error("API is NOT in demo mode — set FINSIGHT_DEMO=1");
        return true;
      },
      30000,
      apiProc,
    ),
  );

  const uiProc = launch("frontend", "npm", ["run", "dev"], FRONTEND);
  await check("frontend listening (:3002)", () => waitUntil("frontend port", () => portListening(UI_PORT), 60000, uiProc));

  // ---- API live-fire: demo-tagged DCF reproduces over HTTP -----------------
  console.log(`API GET /dcf/${DEMO_TICKER}:`);
  const dres = await fetch(`${API}/dcf/${DEMO_TICKER}`, { signal: AbortSignal.timeout(15000) });
  await check(`GET /dcf/${DEMO_TICKER} -> 200`, async () => {
    if (dres.status !== 200) throw new Error(`HTTP ${dres.status}`);
  });
  const dcf = await dres.json();
  await check('DCF response is honestly tagged "mode":"demo"', () => {
    if (dcf.mode !== "demo") throw new Error(`mode ${JSON.stringify(dcf.mode)} !== "demo"`);
  });
  await check(`intrinsic_value_per_share is the deterministic ${EXPECT_INTRINSIC_DISPLAY} (${EXPECT_INTRINSIC})`, () => {
    if (!near(dcf.intrinsic_value_per_share, EXPECT_INTRINSIC, INTRINSIC_TOL))
      throw new Error(`intrinsic_value_per_share ${dcf.intrinsic_value_per_share} not within ${INTRINSIC_TOL} of ${EXPECT_INTRINSIC}`);
    if (`$${Number(dcf.intrinsic_value_per_share).toFixed(2)}` !== EXPECT_INTRINSIC_DISPLAY)
      throw new Error(`rendered form ${`$${Number(dcf.intrinsic_value_per_share).toFixed(2)}`} !== ${EXPECT_INTRINSIC_DISPLAY}`);
  });
  await check("CapEx starts at Apple's reported intensity and the fade is disclosed", () => {
    const path = (dcf.assumptions || {}).capex_pct_schedule || [];
    if (!near(path[0], 9041 / 478834, 1e-9)) throw new Error(`year-1 CapEx ${path[0]} is not the reported 9,041 / 478,834`);
    if (!(dcf.adjustments || {}).capex_as_pct_revenue) throw new Error("CapEx fade not listed in adjustments");
  });
  await check("DCF ran through the real engine (5-year projection, WACC 0.10, tg 0.03)", () => {
    const a = dcf.assumptions || {};
    if (!(Array.isArray(dcf.projected_fcfs_m) && dcf.projected_fcfs_m.length === 5))
      throw new Error(`projected_fcfs_m not a 5-element array`);
    if (!near(a.wacc, 0.1, 1e-9)) throw new Error(`wacc ${a.wacc} !== 0.10`);
    if (!near(a.terminal_growth_rate, 0.03, 1e-9)) throw new Error(`terminal_growth_rate ${a.terminal_growth_rate} !== 0.03`);
    if (a.n_years !== 5) throw new Error(`n_years ${a.n_years} !== 5`);
  });

  // ---- UI live-fire: real browser, real backend, 0 console errors ----------
  console.log("UI (headless Chromium @ :3002):");
  browser = await chromium.launch();
  const page = await (await browser.newContext()).newPage();
  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
  await page.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });

  // The page auto-loads AAPL on mount. The honest "Demo data" badge is the
  // discriminator: it appears ONLY when the backend answered with mode:"demo"
  // (a mock/offline fallback shows "Offline · sample data"; a live backend
  // shows "Live data"). exact:true + waitFor blocks through the cold-start
  // "Loading engine…" flash until the badge settles.
  await check('dashboard flies the honest "Demo data" badge (real demo backend)', async () => {
    await page.getByText("Demo data", { exact: true }).first().waitFor({ timeout: 30000 });
    const body = await page.innerText("body");
    if (body.includes("Live data")) throw new Error('"Live data" badge present — not demo mode');
    if (body.includes("Offline · sample data")) throw new Error("mock fallback rendered — backend was unreachable");
  });

  const uiText = await page.innerText("body");
  await check(`UI renders the DCF intrinsic value ${EXPECT_INTRINSIC_DISPLAY}`, () => {
    if (!uiText.includes("Intrinsic Value")) throw new Error('"Intrinsic Value" label not rendered');
    if (!uiText.includes(EXPECT_INTRINSIC_DISPLAY)) throw new Error(`missing "${EXPECT_INTRINSIC_DISPLAY}"`);
  });
  await check('placeholder sentiment is tagged "Synthetic"; the DCF states its basis', () => {
    // innerText applies the tag's CSS uppercase.
    if (!uiText.includes("SYNTHETIC")) throw new Error('"Synthetic" tag missing on placeholder-transcript sentiment');
    if (!uiText.includes("Basis: FY2026 10-K · FYE 2026-09-26")) throw new Error("DCF basis line missing");
  });
  await check('the DCF panel counts "4 model defaults · 2 adjustments"', () => {
    if (!uiText.includes("4 model defaults · 2 adjustments"))
      throw new Error("defaults/adjustments count missing or wrong");
  });
  // the model value (struck at FYE 2026-09-26) and the
  // price (2026-10-30 close) are dated on both sides of the comparison.
  await check("model vs price dates both sides (valuation date · price close)", () => {
    if (!uiText.includes("valuation date 2026-09-26 · price close 2026-10-30"))
      throw new Error("valuation/price date line missing under Model vs price");
  });
  // EV reconciles to the value on the page, and what the inputs cannot
  // show is on it, not only in the JSON.
  await check("the DCF panel prints net debt, equity value and shares", () => {
    const panel = uiText.split("DCF Valuation")[1]?.split("Earnings Sentiment")[0] ?? "";
    for (const claim of ["Net Debt\n$43.8B", "Equity Value\n$2.4T", "Shares\n14,720.3M"])
      if (!panel.includes(claim)) throw new Error(`missing ${JSON.stringify(claim)}`);
  });
  await check("the page renders the DCF's limitations, the securities exclusion among them", () => {
    if (!uiText.includes("What the DCF's inputs cannot show")) throw new Error("no limitations panel");
    if (!uiText.includes("Marketable securities (current and non-current) are not netted"))
      throw new Error("the securities exclusion is not on the page");
    if (uiText.includes("The demo's debt excludes current debt")) throw new Error("Tesla's note on Apple's page");
  });
  await check("no panel is invented: the charts are the DCF's own, the rest is named as absent", () => {
    if (/\bSample\b/.test(uiText)) throw new Error('a "Sample" tag rendered');
    if (uiText.includes("Peer Comparison") || uiText.includes("Catalysts")) throw new Error("an invented panel rendered");
    if (!uiText.includes("DCF projection: revenue")) throw new Error("DCF projection chart missing");
    if (!uiText.includes("Not on this dashboard")) throw new Error("the absent panels are not named");
  });
  // reported FCF and the DCF's FCF are two figures with two names.
  await check("the two free cash flows carry two names, and the page says how they differ", () => {
    if (!uiText.toUpperCase().includes("REPORTED FCF")) throw new Error("no Reported FCF tile");
    if (!uiText.includes("DCF projection: modelled FCF")) throw new Error("no modelled FCF chart");
    if (/DCF projection: free cash flow/i.test(uiText)) throw new Error("the chart still says free cash flow");
    if (!uiText.includes("stock-based compensation stays a cost")) throw new Error("the difference is not on the page");
  });
  await check("0 console errors", () => {
    if (consoleErrors.length) throw new Error(`${consoleErrors.length}: ${consoleErrors.slice(0, 3).join(" | ")}`);
  });

  // A ticker the demo engine does not bundle: demo mode is
  // offline for EVERY ticker, so the dashboard must say "Not in demo set" and
  // must not ask the engine about it at all.
  const uncoveredCalls = [];
  page.on("request", (r) => {
    if (r.url().includes(`:${API_PORT}/`) && /AMZN/i.test(r.url())) uncoveredCalls.push(r.url());
  });
  await check('a non-bundled ticker (AMZN) shows "Not in demo set" and is never requested', async () => {
    await page.fill("#ticker-search", "AMZN");
    await page.press("#ticker-search", "Enter");
    await page.getByText("Not in demo set", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await page.innerText("body");
    if (!text.includes("AMZN is not in the offline demo set")) throw new Error("banner missing");
    if (text.includes("Demo data") || text.includes("Live data")) throw new Error("a data badge over an uncovered ticker");
    if (uncoveredCalls.length) throw new Error(`engine asked about AMZN: ${uncoveredCalls.join(", ")}`);
  });
  await check("that ticker shows the reason and no figure", async () => {
    const text = await page.innerText("body");
    if (!text.includes("no figure is shown")) throw new Error("no reason given");
    if (/\$\d/.test(text)) throw new Error(`a dollar figure rendered: ${text.match(/\$\d[\d.,]*/)[0]}`);
    if (text.includes("DCF Valuation") || text.includes("Financials")) throw new Error("a panel rendered");
  });

  // Negative control: a failed engine call must not leave a clean badge over
  // a panel with no data. (Its own context: the aborted request logs a console
  // error by design, so it is kept out of the count above.)
  const partialPage = await (await browser.newContext()).newPage();
  await partialPage.route(/:8001\/dcf\//, (route) => route.abort());
  await partialPage.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check('a failed /dcf flies "Partial data" and the DCF panel reads Unavailable', async () => {
    await partialPage.getByText("Partial data", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await partialPage.innerText("body");
    if (text.includes("Demo data") || text.includes("Live data")) throw new Error("a clean badge over a failed /dcf");
    if (!text.includes("Not valued: the /dcf call failed")) throw new Error("the DCF panel does not say the call failed");
    const dcfPanel = text.split("DCF Valuation")[1]?.split("Earnings Sentiment")[0] ?? "";
    if (/\$\d/.test(dcfPanel)) throw new Error("a dollar figure in the failed DCF panel");
    if (!dcfPanel.includes("Unavailable")) throw new Error("the DCF panel does not read Unavailable");
    if (text.includes(EXPECT_INTRINSIC_DISPLAY)) throw new Error("a recorded figure replaced the failed call");
    if (!text.includes("$344.63")) throw new Error("the parts that answered are not shown");
  });

  // no engine at all, which is what a deploy with no backend
  // is. The page replays the recording, says so, and invents nothing.
  const noEngine = await (await browser.newContext()).newPage();
  await noEngine.route(/:8001\//, (route) => route.abort());
  await noEngine.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check("no engine: the badge says it is a recording, and AAPL reads the engine's value", async () => {
    await noEngine.getByText("Engine recording · bundled SEC snapshots · no live backend", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await noEngine.innerText("body");
    if (!text.includes("A recording, not live data.")) throw new Error("banner missing");
    if (!text.includes(EXPECT_INTRINSIC_DISPLAY)) throw new Error(`missing ${EXPECT_INTRINSIC_DISPLAY}`);
    if (text.includes("$220.00") || text.includes("$383.3B") || text.includes("$3.1T")) throw new Error("an invented figure rendered");
    if (!text.includes("SYNTHETIC")) throw new Error("recorded sentiment is not tagged Synthetic");
    if (/\bSample\b/.test(text)) throw new Error('a "Sample" tag rendered');
  });
  await check("no engine: TSLA reads the engine's value", async () => {
    await noEngine.locator(`button[aria-pressed][aria-label="Analyze TSLA"]`).click();
    await noEngine.getByText("$13.19").first().waitFor({ timeout: 30000 });
    const text = await noEngine.innerText("body");
    if (text.includes("$195.00") || text.includes("Bullish")) throw new Error("an invented figure rendered");
  });
  // both of Tesla's share counts and what the second one moves.
  await check("no engine: TSLA states its two share counts and what the choice moves", async () => {
    const text = await noEngine.innerText("body");
    for (const claim of ["3,528M", "3,751M", "$12.41", "$0.78", "5.9%"])
      if (!text.includes(claim)) throw new Error(`missing ${claim}`);
    if (!text.includes("$13.19")) throw new Error("the value on the page moved");
  });
  // while nothing settles Tesla's current debt, its page says it is out.
  await check("no engine: TSLA says its debt excludes current debt, with the filing and the effect", async () => {
    const text = await noEngine.innerText("body");
    for (const claim of ["The demo's debt excludes current debt.", "$1,569M of current debt", "about 3% of the $13.19 value"])
      if (!text.includes(claim)) throw new Error(`missing "${claim}"`);
    await noEngine.locator('a[href*="tsla-20251231.htm"]').first().waitFor({ timeout: 5000 });
  });
  await check("no engine: any other ticker is an empty state with its reason and no figure", async () => {
    await noEngine.fill("#ticker-search", "AMZN");
    await noEngine.press("#ticker-search", "Enter");
    await noEngine.getByText("Offline · no data", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await noEngine.innerText("body");
    if (!text.includes("No data for AMZN.") || !text.includes("no figure is shown")) throw new Error("no reason given");
    if (/\$\d/.test(text)) throw new Error(`a dollar figure rendered: ${text.match(/\$\d[\d.,]*/)[0]}`);
    if (text.includes("DCF Valuation") || text.includes("Financials")) throw new Error("a panel rendered");
  });

  // the engine is up and says why it cannot serve (no SEC identity), so
  // "did not answer" would be false and its remedy lost.
  const REMEDY = "SEC_EDGAR_USER_AGENT is not set. Set it to your own name and email.";
  const refusingPage = await (await browser.newContext()).newPage();
  await refusingPage.route(/:8001\/(meta|kpis|dcf|sentiment)\//, (route) =>
    route.fulfill({ status: 503, headers: { "access-control-allow-origin": "*" }, json: { detail: REMEDY } }));
  await refusingPage.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check("an engine answering 503: the recording's notice carries the engine's remedy", async () => {
    await refusingPage.getByText("A recording, not live data.").first().waitFor({ timeout: 30000 });
    const text = await refusingPage.innerText("body");
    if (!text.includes(`The engine answered but could not serve this ("${REMEDY}")`)) throw new Error("the remedy is not shown");
    if (text.includes("did not answer")) throw new Error('still says "did not answer"');
    if (text.includes("no live backend")) throw new Error('the badge or footer still says "no live backend"');
  });
  await refusingPage.close();

  // a DCF the engine REFUSES is an answer, not a failed call:
  // its reason shows and no number does (a refused JPM read $290.90, EV $4.3T).
  // The CORS header is what the real engine sends on its own 422.
  const REFUSAL = "Cannot value AAPL: diluted shares are not reported.";
  const refusedPage = await (await browser.newContext()).newPage();
  await refusedPage.route(/:8001\/dcf\/AAPL/, (route) =>
    route.fulfill({ status: 422, headers: { "access-control-allow-origin": "*" }, json: { detail: REFUSAL } }));
  await refusedPage.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check("a refused /dcf shows the engine's reason and no number", async () => {
    await refusedPage.getByText("Demo data", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await refusedPage.innerText("body");
    if (!text.includes(REFUSAL)) throw new Error("the engine's reason is not shown");
    if (text.includes(EXPECT_INTRINSIC_DISPLAY))
      throw new Error("a DCF figure shown beside a refusal");
    const dcfPanel = text.split("DCF Valuation")[1]?.split("Earnings Sentiment")[0] ?? "";
    if (dcfPanel.includes("Unavailable")) throw new Error("a refusal shown as a failed call");
  });

  // a figure the engine does not report must read "Not
  // reported" — never a made-up value — and the
  // summary must not quote it. /kpis is answered with two fields nulled.
  const nullPage = await (await browser.newContext()).newPage();
  await nullPage.route(/:8001\/kpis\/AAPL/, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, gross_margin: null, revenue_growth_yoy: null } });
  });
  await nullPage.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check('a null KPI reads "Not reported", never a made-up figure', async () => {
    await nullPage.getByText("Demo data", { exact: true }).first().waitFor({ timeout: 30000 });
    const text = await nullPage.innerText("body");
    const tile = (label) => text.split(label.toUpperCase())[1]?.split("\n").find((l) => l.trim());
    if (tile("Gross Margin")?.trim() !== "Not reported") throw new Error(`gross margin tile: ${tile("Gross Margin")}`);
    if (tile("Revenue Growth")?.trim() !== "Not reported") throw new Error(`growth tile: ${tile("Revenue Growth")}`);
    if (!text.includes("AAPL: revenue growth and gross margin not reported."))
      throw new Error("summary quotes a figure the engine did not report");
  });

  // a slow reply for a ticker already left must not replace
  // the current one. MSFT's /dcf is held 4 s; the user moves on to AAPL.
  const racePage = await (await browser.newContext()).newPage();
  await racePage.route(/:8001\/dcf\/MSFT/, async (route) => {
    await sleep(4000);
    await route.continue().catch(() => {});
  });
  await racePage.goto(UI, { waitUntil: "domcontentloaded", timeout: 60000 });
  await check("a slow reply for the previous ticker is discarded", async () => {
    await racePage.getByText("Demo data", { exact: true }).first().waitFor({ timeout: 30000 });
    await racePage.locator(`button[aria-pressed][aria-label="Analyze MSFT"]`).click();
    await racePage.locator(`button[aria-pressed][aria-label="Analyze AAPL"]`).click();
    await racePage.getByText(EXPECT_INTRINSIC_DISPLAY).first().waitFor({ timeout: 30000 });
    await sleep(5000); // MSFT's reply lands in here
    const text = await racePage.innerText("body");
    if (!text.includes(EXPECT_INTRINSIC_DISPLAY) || text.includes("$313.23"))
      throw new Error("the stale MSFT reply replaced AAPL's figures");
  });
} catch (e) {
  fail++;
  console.log(`\n✗ FATAL — ${e && e.message ? e.message : String(e)}`);
} finally {
  if (browser) {
    try {
      await browser.close();
    } catch {}
  }
  killAll();
}

console.log(`\n${pass} passed, ${fail} failed  (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
process.exit(fail === 0 ? 0 : 1);

/**
 * Capture FinSight's two published images and verify them before writing: every
 * figure visible in the image must be what /meta, /kpis and /dcf return, and no
 * rating, price target, sample or synthetic text may appear.
 *
 *   docs/screenshot.png      the README hero (1440 CSS px wide, @2x, cropped to the
 *                            header, hero card and financial tiles), from the running
 *                            demo stack: its badge reads "Demo data"
 *   frontend/public/og.png   the social-preview card (2400×1260: 1600×840 CSS px at
 *                            3/2 scale; panels below the financial tiles are hidden:
 *                            the sentiment panel is synthetic), from what a shared link
 *                            lands on, a deploy with no backend: its badge says it is a
 *                            recording.
 *
 *   node e2e/capture.mjs screenshot   # or: og | both
 *
 * Each target builds the production frontend it needs, because the API URL is baked in
 * at build time, so nothing else may use frontend/.next meanwhile. It starts the API
 * (FINSIGHT_DEMO=1, :8001; the figures are checked against it in both states) and
 * `next start` (:3002), and stops both on exit. tests/test_docs.py fails if og.png is
 * older in git history than docs/screenshot.png, so the two cannot drift apart.
 */
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { setTimeout as sleep } from "node:timers/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const FRONTEND = join(ROOT, "frontend");
const { chromium } = createRequire(join(FRONTEND, "package.json"))("playwright");
const API = "http://127.0.0.1:8001";
const UI = "http://127.0.0.1:3002";
const which = process.argv[2] || "both";

const TARGETS = {
  screenshot: { out: join(ROOT, "docs/screenshot.png"), width: 1440, height: null, scale: 2,
                apiUrl: "http://localhost:8001", badge: "Demo data", other: "Engine recording",
                says: (health) => [`bundled SEC data for ${health.demo_tickers.join(", ")} only`] },
  og: { out: join(FRONTEND, "public/og.png"), width: 1600, height: 840, scale: 3 / 2,
        apiUrl: "", badge: "Engine recording · bundled SEC snapshots · no live backend", other: "Demo data",
        says: () => ["A recording, not live data.", "This deployment has no backend"] },
};

const kids = [];
function launch(cmd, args, cwd, env) {
  const c = spawn(cmd, args, { cwd, detached: true, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env } });
  c.tail = [];
  const keep = (d) => { c.tail.push(String(d)); if (c.tail.length > 20) c.tail.shift(); };
  c.stdout.on("data", keep); c.stderr.on("data", keep);
  kids.push(c);
  return c;
}
function killAll() {
  for (const c of kids) { try { process.kill(-c.pid, "SIGKILL"); } catch {} }
}
async function until(what, fn, ms) {
  const t = Date.now();
  while (Date.now() - t < ms) { try { if (await fn()) return; } catch {} await sleep(400); }
  throw new Error(`timeout: ${what}`);
}

async function expectedStrings() {
  const j = async (p) => (await fetch(`${API}${p}`)).json();
  const [meta, kpis, dcf, health] = await Promise.all([j("/meta/AAPL"), j("/kpis/AAPL"), j("/dcf/AAPL"), j("/health")]);
  if ([meta, kpis, dcf].some((r) => r.mode !== "demo")) throw new Error("API not in demo mode");
  const pct = (n) => `${n < 0 ? "-" : ""}${Math.abs(n * 100).toFixed(1)}%`;
  const money = (n) => (n >= 1e6 ? `$${(n / 1e6).toFixed(1)}T` : n >= 1e3 ? `$${(n / 1e3).toFixed(1)}B` : `$${n}M`);
  const gap = dcf.intrinsic_value_per_share / meta.current_price - 1;
  return {
    health,
    api: { price: meta.current_price, price_date: meta.price_date, iv: dcf.intrinsic_value_per_share,
           valuation_date: dcf.valuation_date, revenue: kpis.revenue, period: kpis.period },
    strings: [
      `$${meta.current_price.toFixed(2)}`, `close ${meta.price_date} · not a live quote`,
      `$${dcf.intrinsic_value_per_share.toFixed(2)}`, `${gap >= 0 ? "+" : ""}${pct(gap)}`,
      `valuation date ${dcf.valuation_date} · price close ${meta.price_date}`,
      `Financials · ${kpis.period} 10-K (SEC EDGAR)`, `Revenue ${kpis.period}`,
      money(kpis.revenue), pct(kpis.revenue_growth_yoy), pct(kpis.gross_margin),
      pct(kpis.operating_margin), pct(kpis.net_margin), money(kpis.free_cash_flow),
      `$${kpis.eps_diluted.toFixed(2)}`,
      `revenue grew ${(kpis.revenue_growth_yoy * 100).toFixed(1)}% with a ${(kpis.gross_margin * 100).toFixed(0)}% gross margin`,
      `${Math.abs(gap * 100).toFixed(0)}% below the price`,
      `generic ${Math.round(dcf.assumptions.wacc * 100)}% WACC and ${Math.round(dcf.assumptions.terminal_growth_rate * 100)}% terminal growth`,
    ],
  };
}

const FORBIDDEN = ["Sample", "Synthetic", "HOLD", "SELL", "BUY", "PRICE TARGET", "Price target",
                   "UPSIDE", "Offline · sample data", "Live data", "Partial", "Not reported", "INVESTMENT THESIS"];

async function capture(browser, name, t, expected) {
  const page = await (await browser.newContext({
    viewport: { width: t.width, height: t.height || 1000 }, deviceScaleFactor: t.scale,
  })).newPage();
  const errors = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(UI, { waitUntil: "networkidle", timeout: 90000 });
  await page.getByText(t.badge, { exact: true }).first().waitFor({ timeout: 30000 });
  await page.getByText("valuation date", { exact: false }).first().waitFor({ timeout: 30000 });
  await page.evaluate(() => document.fonts.ready);

  const grid = page.locator(".metric-card").first().locator("xpath=..");
  const box = await grid.boundingBox();
  const height = t.height || Math.ceil(box.y + box.height + 16);
  if (box.y + box.height > height) throw new Error(`${name}: the financial tiles do not fit in ${height}px`);
  // Everything after the tiles is hidden for a fixed-size card (it would
  // otherwise peek in under the tiles); the sentiment panel there is synthetic.
  await page.evaluate(() => {
    const tiles = document.querySelector(".metric-card").parentElement;
    for (let n = tiles.nextElementSibling; n; n = n.nextElementSibling) n.style.visibility = "hidden";
  });
  await sleep(500);

  const region = (await page.evaluate((bottom) => {
    const out = [];
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const el = n.parentElement;
      const r = el.getBoundingClientRect();
      if (r.height > 0 && r.top >= 0 && r.top < bottom && getComputedStyle(el).visibility !== "hidden")
        out.push(n.textContent.trim());
    }
    return out.filter(Boolean);
  }, height)).join(" ").replace(/\s+/g, " ");
  const tight = (await page.evaluate((bottom) =>
    [...document.querySelectorAll("header, main > div > *")]
      .filter((e) => e.getBoundingClientRect().bottom <= bottom + 1 && getComputedStyle(e).visibility !== "hidden")
      .map((e) => e.innerText).join("\n"), height)).replace(/\s+/g, " ");
  const hay = `${region}\n${tight}`;
  const missing = [t.badge, ...t.says(expected.health), ...expected.strings].filter((s) => !hay.includes(s));
  const forbidden = [...FORBIDDEN, t.other].filter((s) => region.includes(s));
  console.log(`${name}: numbers visible:`, [...new Set(region.match(/[-+$]?\d[\d,.]*%?[BTM]?/g) || [])].join("  "));
  if (missing.length || forbidden.length || errors.length)
    throw new Error(`${name}: verification failed — missing ${JSON.stringify(missing)}, ` +
                    `forbidden ${JSON.stringify(forbidden)}, console errors ${errors.length}`);
  await page.screenshot({ path: t.out, clip: { x: 0, y: 0, width: t.width, height } });
  console.log(`${name}: wrote ${t.out} (${t.width}x${height} CSS px @${t.scale.toFixed(3)}x)`);
}

let browser, code = 1;
try {
  launch("python3", ["-m", "uvicorn", "finsight.api.app:app", "--host", "127.0.0.1", "--port", "8001"], ROOT, { FINSIGHT_DEMO: "1" });
  await until("API", async () => (await (await fetch(`${API}/health`)).json()).demo_mode === true, 60000);
  const expected = await expectedStrings();
  console.log("API:", JSON.stringify(expected.api));
  browser = await chromium.launch();
  for (const name of which === "both" ? ["screenshot", "og"] : [which]) {
    const target = TARGETS[name];
    console.log(`${name}: building the frontend (NEXT_PUBLIC_API_URL=${target.apiUrl || "unset"})`);
    const built = spawnSync("npm", ["run", "build"], { cwd: FRONTEND, stdio: "ignore",
      env: { ...process.env, NEXT_PUBLIC_API_URL: target.apiUrl } });
    if (built.status !== 0) throw new Error(`${name}: next build failed`);
    const ui = launch("npm", ["start"], FRONTEND, {});
    await until("UI", async () => (await fetch(UI)).ok, 90000);
    await capture(browser, name, target, expected);
    try { process.kill(-ui.pid, "SIGKILL"); } catch {}
    await until("UI stopped", async () => { try { await fetch(UI); return false; } catch { return true; } }, 15000);
  }
  code = 0;
} catch (e) {
  console.log("FAIL:", e.message);
} finally {
  if (browser) await browser.close().catch(() => {});
  killAll();
}
process.exit(code);

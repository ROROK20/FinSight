/**
 * Build-time URL resolution: a production build without NEXT_PUBLIC_API_URL /
 * NEXT_PUBLIC_SITE_URL must not point visitors at localhost.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTs } from "./load-ts.mjs";

const { resolveApiUrl, tickerPath } = await loadTs("lib/api.ts");
const { resolveSiteUrl } = await loadTs("lib/site.ts");

test("the engine URL: configured, dev-only localhost, or none", () => {
  assert.equal(resolveApiUrl({ NEXT_PUBLIC_API_URL: "https://api.example.org/", NODE_ENV: "production" }),
               "https://api.example.org");
  assert.equal(resolveApiUrl({ NODE_ENV: "development" }), "http://localhost:8001");
  // Production without the variable: no engine (offline sample state), never localhost.
  assert.equal(resolveApiUrl({ NODE_ENV: "production" }), null);
  assert.equal(resolveApiUrl({ NEXT_PUBLIC_API_URL: "  ", NODE_ENV: "production" }), null);
});

test("the site URL for share cards: configured, dev-only localhost, or omitted", () => {
  assert.equal(resolveSiteUrl({ NEXT_PUBLIC_SITE_URL: "https://finsight.example.org", NODE_ENV: "production" }).href,
               "https://finsight.example.org/");
  assert.equal(resolveSiteUrl({ NODE_ENV: "development" }).href, "http://localhost:3002/");
  assert.equal(resolveSiteUrl({ NODE_ENV: "production" }), undefined);
});

test("a ticker is validated and URL-encoded before it is a path segment", () => {
  // "AAPL/../TSLA" fetched /kpis/TSLA under the AAPL header.
  assert.equal(tickerPath("AAPL/../TSLA"), null);
  assert.equal(tickerPath("A.."), null);
  assert.equal(tickerPath("AAPL?x=1"), null);
  assert.equal(tickerPath(" aapl "), "AAPL");
  assert.equal(tickerPath("BRK.B"), "BRK.B");
  assert.equal(tickerPath("BF-B"), "BF-B");
});

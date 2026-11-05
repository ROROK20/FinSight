/**
 * api.dcf keeps the engine's reason when it refuses a ticker, so a refused DCF does
 * not look like a failed call.
 */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_URL = "http://engine.test";
const { api, refusalReason } = await (await import("./load-ts.mjs")).loadTs("lib/api.ts");

const answer = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => {
    if (body === undefined) throw new SyntaxError("not JSON");
    return body;
  },
});

const REASON = "JPM: diluted shares not reported; no placeholder fundamentals are used.";
let calls;
beforeEach(() => { calls = []; });
const engineAnswers = (res) => {
  globalThis.fetch = async (url) => { calls.push(String(url)); return res; };
};

test("a 422 keeps the reason and no valuation", async () => {
  engineAnswers(answer(422, { detail: REASON }));
  assert.deepEqual(await api.dcf("JPM"), { dcf: null, refusal: REASON });
  assert.deepEqual(calls, ["http://engine.test/dcf/JPM"]);
});

test("a 404 from the engine is a stated refusal too", async () => {
  engineAnswers(answer(404, { detail: "No DCF data available for ZZZZ." }));
  assert.equal((await api.dcf("ZZZZ")).refusal, "No DCF data available for ZZZZ.");
});

test("a valuation passes through", async () => {
  const dcf = { ticker: "AAPL", mode: "demo", intrinsic_value_per_share: 106.7053 };
  engineAnswers(answer(200, dcf));
  assert.deepEqual(await api.dcf("AAPL"), { dcf, refusal: null });
});

test("anything else is a failed call, not a refusal", async () => {
  for (const res of [answer(500, { detail: "An unexpected error occurred." }),
                     answer(503, { detail: "Set SEC_EDGAR_USER_AGENT." }),
                     answer(404),                      // a proxy's HTML error page
                     answer(422, { detail: [{ loc: ["path"] }] })]) {
    engineAnswers(res);
    assert.deepEqual(await api.dcf("JPM"), { dcf: null, refusal: null });
  }
  globalThis.fetch = async () => { throw new TypeError("network down"); };
  assert.deepEqual(await api.dcf("JPM"), { dcf: null, refusal: null });
});

test("not a ticker: no request, no refusal", async () => {
  engineAnswers(answer(422, { detail: REASON }));
  assert.deepEqual(await api.dcf("AAPL/../TSLA"), { dcf: null, refusal: null });
  assert.deepEqual(calls, []);
});

test("refusalReason reads only 422 and 404", async () => {
  assert.equal(await refusalReason(answer(422, { detail: REASON })), REASON);
  assert.equal(await refusalReason(answer(400, { detail: REASON })), null);
  assert.equal(await refusalReason(answer(422, { detail: "" })), null);
});

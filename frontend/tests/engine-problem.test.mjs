/**
 * An engine that answers a data call with a server error has said something: the page keeps
 * the remedy the engine stated (no SEC_EDGAR_USER_AGENT, say).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_URL = "http://engine.test";
const { api, engineProblem, noDataReason } = await (await import("./load-ts.mjs")).loadTs("lib/api.ts");

const answer = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => {
    if (body === undefined) throw new SyntaxError("not JSON");
    return body;
  },
});

const REMEDY = "SEC_EDGAR_USER_AGENT is not set. Set it to your own name and email.";
const engineAnswers = (res) => { globalThis.fetch = async () => res; };

test("a server error's stated detail is what the engine said", async () => {
  assert.equal(await engineProblem(answer(503, { detail: REMEDY })), REMEDY);
  assert.equal(await engineProblem(answer(500, { detail: "An unexpected error occurred." })),
    "An unexpected error occurred.");
});

test("anything else says nothing", async () => {
  assert.equal(await engineProblem(answer(502)), null);                   // a proxy's HTML page
  assert.equal(await engineProblem(answer(503, { detail: [{ loc: [] }] })), null);
  assert.equal(await engineProblem(answer(503, { detail: "" })), null);
  assert.equal(await engineProblem(answer(404, { detail: "Not here." })), null);
  assert.equal(await engineProblem(answer(422, { detail: "Refused." })), null);
});

test("api.meta, api.kpis and api.sentiment still resolve to null, and report the answer", async () => {
  engineAnswers(answer(503, { detail: REMEDY }));
  for (const call of ["meta", "kpis", "sentiment"]) {
    const heard = [];
    const out = await api[call]("AAPL", async (res) => { heard.push(await engineProblem(res)); });
    assert.equal(out, null, call);
    assert.deepEqual(heard, [REMEDY], call);
  }
});

test("api.dcf reports a 503 and keeps a refusal to itself", async () => {
  const heard = [];
  const onFailed = async (res) => { heard.push(await engineProblem(res)); };
  engineAnswers(answer(503, { detail: REMEDY }));
  assert.deepEqual(await api.dcf("AAPL", onFailed), { dcf: null, refusal: null });
  assert.deepEqual(heard, [REMEDY]);
  heard.length = 0;
  engineAnswers(answer(422, { detail: "Refused." }));
  assert.deepEqual(await api.dcf("AAPL", onFailed), { dcf: null, refusal: "Refused." });
  assert.deepEqual(heard, []);
});

test("the notice carries what the engine said, and is otherwise as it was", () => {
  assert.equal(noDataReason(REMEDY, "http://engine.test"),
    `The engine answered but could not serve this ("${REMEDY}")`);
  assert.equal(noDataReason(null, "http://engine.test"), "The configured backend did not answer");
  assert.equal(noDataReason(null, null), "This deployment has no backend");
});

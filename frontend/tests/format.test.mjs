/** Negative figures: sign before the currency, scaled like positives. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTs } from "./load-ts.mjs";

const { fmtMoney, fmtUsd, fmtPct } = await loadTs("lib/format.ts");

test("negative money keeps its sign before $ and scales", () => {
  assert.equal(fmtMoney(-50_000), "-$50.0B");
  assert.equal(fmtMoney(-2_500_000), "-$2.5T");
  assert.equal(fmtMoney(-750), "-$750M");
  assert.equal(fmtUsd(-13.85), "-$13.85");
  assert.equal(fmtPct(-0.029), "-2.9%");
});

test("positive figures are unchanged (the demo tiles)", () => {
  assert.equal(fmtMoney(416161), "$416.2B");
  assert.equal(fmtMoney(98767), "$98.8B");
  assert.equal(fmtUsd(106.7053), "$106.71");
  assert.equal(fmtUsd(344.63), "$344.63");
  assert.equal(fmtPct(0.469089), "46.9%");
});

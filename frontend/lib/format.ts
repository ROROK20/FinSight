/**
 * Number formatting for the dashboard. Negatives keep their sign BEFORE the
 * currency and scale like positives: -$50.0B, -$13.85. No runtime imports, so the
 * unit tests can load it directly.
 */

const MINUS = "-"; // ASCII hyphen-minus, as the page prints "-52.0%"

const sign = (v: number) => (v < 0 ? MINUS : "");

/** A ratio as a percentage: 0.469 → "46.9%", -0.029 → "-2.9%". */
export function fmtPct(v: number): string {
  return `${sign(v)}${Math.abs(v * 100).toFixed(1)}%`;
}

/** A per-share or price figure: 165.3101 → "$165.31", -13.85 → "-$13.85". */
export function fmtUsd(v: number): string {
  return `${sign(v)}$${Math.abs(v).toFixed(2)}`;
}

/** A figure in $M, scaled: 478834 → "$478.8B", -50000 → "-$50.0B". */
export function fmtMoney(v: number): string {
  const a = Math.abs(v);
  const body =
    a >= 1_000_000 ? `$${(a / 1_000_000).toFixed(1)}T` :
    a >= 1_000 ? `$${(a / 1_000).toFixed(1)}B` :
    `$${a.toLocaleString("en-US")}M`;
  return `${sign(v)}${body}`;
}

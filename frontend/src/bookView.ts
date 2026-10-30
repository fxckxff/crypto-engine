import type { RawLevel } from "./types";

/** Price buckets are integer arithmetic; volume sums are display-only. */
export function bucketPrice(price: string, tick: string, multiplier: number, decimals: number, side: "BID" | "ASK"): string {
  if (multiplier === 1) return price;
  const units = (text: string) => {
    const [whole = "0", fraction = ""] = text.split(".");
    return BigInt(whole + fraction.padEnd(decimals, "0").slice(0, decimals));
  };
  const step = units(tick) * BigInt(multiplier);
  if (step <= 0n) return price;
  const value = units(price);
  const bucket = (side === "BID" ? value / step : (value + step - 1n) / step) * step;
  const text = bucket.toString().padStart(decimals + 1, "0");
  return decimals ? `${text.slice(0, -decimals)}.${text.slice(-decimals)}` : text;
}

export function groupLevels(levels: RawLevel[], tick: string, multiplier: number, decimals: number, side: "BID" | "ASK"): RawLevel[] {
  if (multiplier === 1) return levels;
  const groups = new Map<string, [number, number]>();
  for (const [price, quantity, notional] of levels) {
    const key = bucketPrice(price, tick, multiplier, decimals, side);
    const previous = groups.get(key) ?? [0, 0];
    groups.set(key, [previous[0] + Number(quantity), previous[1] + Number(notional)]);
  }
  return [...groups].map(([price, [quantity, notional]]) => [price, String(quantity), String(notional)]);
}

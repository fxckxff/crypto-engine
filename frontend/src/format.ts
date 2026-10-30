const priceFormatters = new Map<number, Intl.NumberFormat>();

function priceFormatter(decimals: number): Intl.NumberFormat {
  const cached = priceFormatters.get(decimals);
  if (cached) {
    return cached;
  }
  const formatter = new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  priceFormatters.set(decimals, formatter);
  return formatter;
}

export function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Количество знаков после запятой, достаточное для отображения шага цены. */
export function decimalsForStep(step: string | null | undefined): number {
  if (!step) {
    return 2;
  }
  const stepValue = toNumber(step);
  if (stepValue === null || stepValue <= 0) {
    return 2;
  }
  const text = step.replace(/0+$/, "");
  const dot = text.indexOf(".");
  return dot === -1 ? 0 : Math.min(text.length - dot - 1, 8);
}

export function fmtPrice(value: string | number | null | undefined, decimals = 2): string {
  const numeric = toNumber(value);
  if (numeric === null) {
    return "—";
  }
  return priceFormatter(decimals).format(numeric);
}

/** Объём в базовом активе: значимые цифры без экспоненты. */
export function fmtQty(value: string | number | null | undefined): string {
  const numeric = toNumber(value);
  if (numeric === null) {
    return "—";
  }
  const abs = Math.abs(numeric);
  if (abs >= 1000) {
    return numeric.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
  }
  if (abs >= 1) {
    return numeric.toLocaleString("ru-RU", { maximumFractionDigits: 3 });
  }
  return numeric.toLocaleString("ru-RU", { maximumFractionDigits: 5 });
}

export function fmtUsd(value: string | number | null | undefined): string {
  const numeric = toNumber(value);
  if (numeric === null) {
    return "—";
  }
  const abs = Math.abs(numeric);
  if (abs >= 1_000_000) {
    return `${(numeric / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} млн`;
  }
  if (abs >= 10_000) {
    return `${(numeric / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} тыс.`;
  }
  return numeric.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}

export function fmtPct(value: string | number | null | undefined, digits = 3): string {
  const numeric = toNumber(value);
  if (numeric === null) {
    return "—";
  }
  return `${numeric.toFixed(digits)}%`;
}

export function fmtSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) {
    return "—";
  }
  if (ms < 1000) {
    return `${Math.round(ms)} мс`;
  }
  const seconds = ms / 1000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)} с`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds - minutes * 60)} с`;
}

export function midPrice(bestBid: string | undefined, bestAsk: string | undefined): number | null {
  const bid = toNumber(bestBid);
  const ask = toNumber(bestAsk);
  if (bid === null || ask === null) {
    return null;
  }
  return (bid + ask) / 2;
}

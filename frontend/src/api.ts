import type { Instrument, MarketPayload, ScannerPayload, SystemStatus } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(8000), cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${path}: HTTP ${response.status} ${text.slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

export function fetchInstruments(): Promise<Instrument[]> {
  return getJson<Instrument[]>("/api/instruments");
}

export function fetchStatus(): Promise<SystemStatus> {
  return getJson<SystemStatus>("/api/system/status");
}

export function fetchOrderBook(symbol: string): Promise<MarketPayload> {
  return getJson<MarketPayload>(`/api/orderbook/${encodeURIComponent(symbol)}`);
}

export function fetchScanner(): Promise<ScannerPayload[]> {
  return getJson<ScannerPayload[]>("/api/scanner/results");
}

/** URL WebSocket того же происхождения, что и страница (в деве — через прокси Vite). */
export function marketSocketUrl(symbol: string): string {
  const explicit = import.meta.env.VITE_WS_BASE;
  if (explicit) {
    return `${explicit}/ws/market/${encodeURIComponent(symbol)}`;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/market/${encodeURIComponent(symbol)}`;
}

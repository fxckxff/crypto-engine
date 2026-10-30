// Типы повторяют контракт backend/app/services/market_service.py::symbol_payload.
// Числа цены/объёма приходят строками: Decimal на бэкенде не должен терять точность
// при сериализации через float.

export type BookState = "EMPTY" | "SYNCING" | "READY" | "STALE" | "INVALID";
export type Side = "BID" | "ASK";

/** [price, quantity, notional_usdt] — все значения строками. */
export type RawLevel = [string, string, string];

export interface SpreadPayload {
  value: number | null;
  pct: number | null;
  bps: number | null;
  ticks: number | null;
  percentile: number | null;
  median: number | null;
}

export interface MicropricePayload {
  value: number | null;
  dev_bps: number | null;
  velocity_bps_s: number | null;
}

export interface OfiPayload {
  top: number | null;
  multi: number | null;
  rolling: number | null;
}

export interface FlowPayload {
  buy_volume: string;
  sell_volume: string;
  cvd: string;
  trades: number;
  classified_share: number;
}

export interface WallPayload {
  side: Side;
  price: string;
  state: string;
  score: number;
  notional: string;
  initial_quantity: string;
  current_quantity?: string;
  qualified?: boolean;
  quality_reasons?: string[];
  size_ratio?: number;
  cancelled_estimate?: string;
  replenished_estimate?: string;
  absorbed: string;
  age_ms: number;
  distance_pct: number;
}

export interface LatencyPayload {
  book_age_ms: number;
  last_exchange_ts_ms: number | null;
}

export interface ScannerComponents {
  spread_opportunity: number;
  execution_probability: number;
  liquidity_quality: number;
  adverse_selection_cost: number;
  volatility_risk: number;
  total_cost: number;
}

export interface ScannerPayload {
  symbol: string;
  eligible: boolean;
  exclude_reason: string | null;
  spread_pct: number | null;
  spread_ticks: number | null;
  bid_depth_usdt: string;
  ask_depth_usdt: string;
  depth_imbalance: number | null;
  book_state: BookState;
  opportunity_score: number;
  components: ScannerComponents | null;
  volume_24h_usdt: string | null;
  wall_count: number;
  top_wall_score: number;
  notes: string[];
  quote_age_ms: number | null;
}

export interface MarketPayload {
  symbol: string;
  ts_ns: number;
  book_state: BookState;
  book_age_ms: number;
  tradable: boolean;
  block_reason: string | null;
  bids: RawLevel[];
  asks: RawLevel[];
  spread: SpreadPayload;
  obi: Record<string, number | null>;
  obi_weighted: number | null;
  microprice: MicropricePayload;
  ofi: OfiPayload;
  flow: FlowPayload;
  walls: WallPayload[];
  latency_ms: LatencyPayload;
  server_ts_ms: number;
  scanner: ScannerPayload | null;
}

export interface RecorderStats {
  recorded: number;
  dropped: number;
  files: number;
  bytes: number;
  queue: number;
  last_error: string | null;
}

export interface BookStat {
  state: BookState;
  levels: number;
  age_ms: number;
  gaps: number;
  resyncs: number;
  dropped_diffs: number;
  trades: number;
}

export interface SystemStatus {
  mode: string;
  exchange: string;
  market: string;
  uptime_s: number;
  running: boolean;
  data_loss: boolean;
  symbols: string[];
  connection: {
    connected: boolean;
    connects: number;
    disconnects: number;
    last_error: string | null;
  };
  recorder: {
    raw: RecorderStats | null;
    features: RecorderStats | null;
  };
  books: Record<string, BookStat>;
}

export interface Instrument {
  symbol: string;
  base_asset: string;
  quote_asset: string;
  status: string;
  trading_allowed: boolean;
  tick_size: string;
  step_size: string;
  min_quantity: string;
  min_notional: string;
}

export type StreamStatus = "connecting" | "open" | "reconnecting" | "closed";

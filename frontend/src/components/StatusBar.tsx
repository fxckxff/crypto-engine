import { fmtDuration } from "../format";
import type { StreamStatus, SystemStatus } from "../types";

interface StatusBarProps {
  status: SystemStatus | null;
  statusError: string | null;
  streamStatus: StreamStatus;
  streamError: string | null;
  lastUpdateAt: number | null;
  symbol: string | null;
  symbols: string[];
  onSymbolChange: (symbol: string) => void;
}

const STREAM_LABELS: Record<StreamStatus, string> = {
  connecting: "подключение",
  open: "поток открыт",
  reconnecting: "переподключение",
  closed: "закрыт",
};

function ago(ts: number | null): string {
  if (ts === null) {
    return "—";
  }
  const delta = Date.now() - ts;
  return delta < 1000 ? "только что" : `${(delta / 1000).toFixed(1)} с назад`;
}

export function StatusBar({
  status,
  statusError,
  streamStatus,
  streamError,
  lastUpdateAt,
  symbol,
  symbols,
  onSymbolChange,
}: StatusBarProps) {
  const connectionOk = status?.connection.connected ?? false;
  const book = symbol && status ? status.books[symbol] : undefined;

  return (
    <header className="status">
      <div className="status__title">
        <h1>Crypto Liquidity Engine</h1>
        <span className="status__subtitle">
          режим: <b>{status?.mode ?? "—"}</b> · {status?.exchange ?? "—"} {status?.market ?? ""}
        </span>
      </div>

      <div className="status__pills">
        <span className={`pill ${connectionOk ? "pill--ok" : "pill--bad"}`}>
          биржа: {connectionOk ? "подключена" : "нет соединения"}
        </span>
        <span className={`pill ${streamStatus === "open" ? "pill--ok" : "pill--warn"}`}>
          {STREAM_LABELS[streamStatus]}
        </span>
        <span className={`pill ${status?.data_loss ? "pill--bad" : "pill--ok"}`}>
          потеря данных: {status ? (status.data_loss ? "есть" : "нет") : "—"}
        </span>
        <span className="pill">uptime {fmtDuration((status?.uptime_s ?? 0) * 1000)}</span>
        {book ? (
          <span className={`pill ${book.state === "READY" ? "pill--ok" : "pill--warn"}`}>
            стакан: {book.state} · {book.levels} ур.
          </span>
        ) : null}
        <span className="pill">обновление: {ago(lastUpdateAt)}</span>
      </div>

      <div className="status__controls">
        <label htmlFor="symbol-select">Символ</label>
        <select
          id="symbol-select"
          value={symbol ?? ""}
          onChange={(event) => onSymbolChange(event.target.value)}
          disabled={symbols.length === 0}
        >
          {symbols.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </div>

      {statusError || streamError ? (
        <p className="status__error">Ошибка: {statusError ?? streamError}</p>
      ) : null}
    </header>
  );
}

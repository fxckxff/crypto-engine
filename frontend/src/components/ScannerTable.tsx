import { fmtPct, fmtUsd } from "../format";
import type { ScannerPayload } from "../types";

interface ScannerTableProps {
  rows: ScannerPayload[];
  selectedSymbol: string | null;
  onSelect: (symbol: string) => void;
}

export function ScannerTable({ rows, selectedSymbol, onSelect }: ScannerTableProps) {
  return (
    <section className="card">
      <header className="card__head">
        <h2>Сканер инструментов</h2>
        <span className="card__hint">Opportunity Score — ранжирование, не прогноз доходности</span>
      </header>

      {rows.length === 0 ? (
        <p className="empty">Сканер ещё не рассчитал ни одного инструмента.</p>
      ) : (
        <table className="table table--clickable">
          <thead>
            <tr>
              <th>Символ</th>
              <th>Спред %</th>
              <th>Тиков</th>
              <th>24h объём</th>
              <th>BID depth</th>
              <th>ASK depth</th>
              <th>Стены</th>
              <th>Opportunity</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.symbol}
                className={row.symbol === selectedSymbol ? "is-selected" : undefined}
                onClick={() => onSelect(row.symbol)}
              >
                <td>
                  <b>{row.symbol}</b>
                </td>
                <td>{fmtPct(row.spread_pct, 3)}</td>
                <td>{row.spread_ticks === null ? "—" : row.spread_ticks.toFixed(2)}</td>
                <td>{row.volume_24h_usdt === null ? "—" : fmtUsd(row.volume_24h_usdt)}</td>
                <td>{fmtUsd(row.bid_depth_usdt)}</td>
                <td>{fmtUsd(row.ask_depth_usdt)}</td>
                <td>{row.wall_count}</td>
                <td>{row.opportunity_score.toFixed(1)}</td>
                <td>
                  {row.eligible ? (
                    <span className="state state--persistent">допущен</span>
                  ) : (
                    <span className="state state--expired" title={row.exclude_reason ?? undefined}>
                      исключён
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {rows.some((row) => row.exclude_reason) ? (
        <ul className="notes">
          {rows
            .filter((row) => row.exclude_reason)
            .map((row) => (
              <li key={`reason-${row.symbol}`}>
                <b>{row.symbol}</b>: {row.exclude_reason}
              </li>
            ))}
        </ul>
      ) : null}
    </section>
  );
}

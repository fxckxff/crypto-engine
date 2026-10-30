import { fmtDuration, fmtPct, fmtPrice, fmtQty, fmtUsd } from "../format";
import type { WallPayload } from "../types";

interface WallsPanelProps {
  walls: WallPayload[];
  decimals: number;
}

export function WallsPanel({ walls, decimals }: WallsPanelProps) {
  return (
    <section className="card">
      <header className="card__head">
        <h2>Качество лимитных стен</h2>
        <span className="card__hint">
          score — эвристика, не подтверждённая статистикой
        </span>
      </header>

      {walls.length === 0 ? (
        <p className="empty">
          Стены не обнаружены. Условия: notional ≥ порога, отношение к соседним уровням ≥ порога,
          расстояние от mid в пределах лимита.
        </p>
      ) : (
        <div style={{ overflowX: "auto" }}><table className="table">
          <thead>
            <tr>
              <th>Сторона</th>
              <th>Цена</th>
              <th>Объём</th>
              <th>Notional</th>
              <th>Состояние</th>
              <th>Качество / Score</th><th>К фону</th><th>Снято, оценка</th>
              <th>Поглощено</th>
              <th>Возраст</th>
              <th>До mid</th>
            </tr>
          </thead>
          <tbody>
            {walls.map((wall) => (
              <tr key={`${wall.side}-${wall.price}`}>
                <td className={wall.side === "BID" ? "value-bid" : "value-ask"}>LIMIT {wall.side}</td>
                <td>{fmtPrice(wall.price, decimals)}</td>
                <td>{fmtQty(wall.current_quantity ?? wall.initial_quantity)}</td>
                <td>{fmtUsd(wall.notional)}</td>
                <td>
                  <span className={`state state--${wall.state.toLowerCase()}`}>{wall.state}</span>
                </td>
                <td title={wall.quality_reasons?.join("; ")}>{wall.qualified ? "Подтверждена" : "Кандидат"} · {wall.score.toFixed(0)}
                  <small style={{ display: "block", maxWidth: "260px" }}>{wall.quality_reasons?.join("; ")}</small></td>
                <td>×{wall.size_ratio?.toFixed(1) ?? "—"}</td>
                <td>{fmtQty(wall.cancelled_estimate ?? "0")}</td>
                <td>{fmtQty(wall.absorbed)}</td>
                <td>{fmtDuration(wall.age_ms)}</td>
                <td>{fmtPct(wall.distance_pct, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </section>
  );
}

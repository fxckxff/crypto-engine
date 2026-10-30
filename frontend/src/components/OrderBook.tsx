import { useMemo, useState, type CSSProperties } from "react";
import { fmtPrice, fmtQty, fmtUsd, toNumber } from "../format";
import { bucketPrice, groupLevels } from "../bookView";
import type { RawLevel, WallPayload } from "../types";

interface OrderBookProps {
  bids: RawLevel[];
  asks: RawLevel[];
  decimals: number;
  tickSize: string;
  walls: WallPayload[];
}

interface Row {
  price: string;
  quantity: string;
  notional: string;
  ratio: number;
  wallScore: number | null;
  wallQualified: boolean;
}

function levelRows(levels: RawLevel[], maximum: number): Row[] {
  return levels.map(([price, quantity, notional]) => ({ price, quantity, notional,
    ratio: maximum > 0 ? (toNumber(notional) ?? 0) / maximum : 0, wallScore: null, wallQualified: false }));
}

function mergeWalls(rows: Row[], walls: WallPayload[], side: "BID" | "ASK"): Row[] {
  const scores = new Map<string, number>();
  for (const wall of walls) {
    if (wall.side === side) {
      scores.set(Number(wall.price).toString(), Math.max(scores.get(Number(wall.price).toString()) ?? 0, wall.score));
    }
  }
  if (scores.size === 0) {
    return rows;
  }
  return rows.map((row) => {
    const exact = scores.get(Number(row.price).toString());
    if (exact !== undefined) {
      return { ...row, wallScore: exact, wallQualified: walls.some(w => w.side === side && Number(w.price) === Number(row.price) && w.qualified) };
    }
    return row;
  });
}

function LevelRow({ row, side, decimals }: { row: Row; side: "BID" | "ASK"; decimals: number }) {
  const wallTitle = row.wallScore !== null ? `${row.wallQualified ? "Подтверждённая наблюдениями стена" : "Кандидат в стену"}, score ${row.wallScore.toFixed(0)}` : undefined;
  return (
    <div className={`ob-row ob-row--${side.toLowerCase()}${row.wallScore !== null ? " ob-row--wall" : ""}`} title={wallTitle}>
      <div className="ob-row__bar" style={{ width: `${Math.min(row.ratio * 100, 100)}%` }} />
      <span className="ob-row__price">
        {row.wallScore !== null ? <b className="wall-mark">{row.wallQualified ? "◆" : "△"}</b> : null}
        {fmtPrice(row.price, decimals)}
      </span>
      <span className="ob-row__qty">{fmtQty(row.quantity)}</span>
      <span className="ob-row__notional">{fmtUsd(row.notional)}</span>
    </div>
  );
}

export function OrderBook({ bids, asks, decimals, tickSize, walls }: OrderBookProps) {
  const [zoom, setZoom] = useState(100);
  const [multiplier, setMultiplier] = useState(1);
  const [visibleLevels, setVisibleLevels] = useState(20);
  const groupedWalls = useMemo(() => walls.map(wall => ({ ...wall,
    price: bucketPrice(wall.price, tickSize, multiplier, decimals, wall.side),
  })), [walls, tickSize, multiplier, decimals]);
  const { bidRows, askRows } = useMemo(() => {
    const bid = groupLevels(bids, tickSize, multiplier, decimals, "BID").slice(0, visibleLevels);
    const ask = groupLevels(asks, tickSize, multiplier, decimals, "ASK").slice(0, visibleLevels);
    const maximum = Math.max(0, ...[...bid, ...ask].map(level => toNumber(level[2]) ?? 0));
    return { bidRows: mergeWalls(levelRows(bid, maximum), groupedWalls, "BID"),
      askRows: mergeWalls(levelRows(ask, maximum), groupedWalls, "ASK") };
  }, [bids, asks, tickSize, multiplier, decimals, visibleLevels, groupedWalls]);

  // The spread always uses real top-of-book prices, never rounded bucket boundaries.
  const bestBid = bids[0] ? { price: bids[0][0] } : undefined;
  const bestAsk = asks[0] ? { price: asks[0][0] } : undefined;
  const spread =
    bestBid && bestAsk ? (toNumber(bestAsk.price) ?? 0) - (toNumber(bestBid.price) ?? 0) : null;
  const spreadPct =
    spread !== null && bestBid && bestAsk
      ? (spread / (((toNumber(bestAsk.price) ?? 0) + (toNumber(bestBid.price) ?? 0)) / 2)) * 100
      : null;

  return (
    <section className="card ob" style={{ "--ob-scale": zoom / 100 } as CSSProperties}>
      <header className="card__head">
        <h2>Стакан</h2>
        <span className="card__hint">
          объёмы — L2, исполнение/отмена на уровне — <b>оценка</b>
        </span>
      </header>

      <div className="ob__controls">
        <div className="ob__zoom" role="group" aria-label="Масштаб стакана">
          <button type="button" aria-label="Уменьшить стакан" disabled={zoom <= 75} onClick={() => setZoom(value => value - 25)}>−</button>
          <output aria-live="polite">{zoom}%</output>
          <button type="button" aria-label="Увеличить стакан" disabled={zoom >= 200} onClick={() => setZoom(value => value + 25)}>+</button>
        </div>
        <label>Шаг цены<select value={multiplier} onChange={event => setMultiplier(Number(event.target.value))}>
          {[1, 2, 5, 10, 25, 50, 100].map(value => <option key={value} value={value}>{fmtPrice(Number(tickSize) * value, decimals)} · ×{value}</option>)}
        </select></label>
        <label>Строк на сторону<select value={visibleLevels} onChange={event => setVisibleLevels(Number(event.target.value))}>
          {[5, 10, 20].map(value => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <button type="button" onClick={() => { setZoom(100); setMultiplier(1); setVisibleLevels(20); }}>Сбросить</button>
      </div>
      <p className="ob__view-note">Полоса показывает объём одного уровня в USDT. Шкала общая для BID и ASK. Получено уровней: {bids.length} bid / {asks.length} ask.
        {multiplier > 1 ? " Суммы внутри ценовых групп; цена — внешняя граница. Более глубокие уровни не подгружаются." : ""}</p>
      <div className="ob__columns">
        <span>Цена</span>
        <span>Объём</span>
        <span>Объём, USDT</span>
      </div>

      <div className="ob__asks">
        {askRows.length === 0 ? <p className="ob__empty">нет данных по ask</p> : null}
        {[...askRows].reverse().map((row) => (
          <LevelRow key={`ask-${row.price}`} row={row} side="ASK" decimals={decimals} />
        ))}
      </div>

      <div className="ob__spread">
        <span>
          BID <b>{bestBid ? fmtPrice(bestBid.price, decimals) : "—"}</b>
        </span>
        <span className="ob__spread-mid">
          spread {spread !== null ? spread.toFixed(decimals) : "—"}
          {spreadPct !== null ? ` · ${spreadPct.toFixed(3)}%` : ""}
        </span>
        <span>
          ASK <b>{bestAsk ? fmtPrice(bestAsk.price, decimals) : "—"}</b>
        </span>
      </div>

      <div className="ob__bids">
        {bidRows.length === 0 ? <p className="ob__empty">нет данных по bid</p> : null}
        {bidRows.map((row) => (
          <LevelRow key={`bid-${row.price}`} row={row} side="BID" decimals={decimals} />
        ))}
      </div>
    </section>
  );
}

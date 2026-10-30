import { useEffect, useState } from "react";
type Row = { symbol: string; price: string; change_pct: number; volume_usdt: number; trades: number;
  spread_pct: number | null; top_depth_usdt: number; monitored: boolean };
type Data = { updated_ns: number; error: string | null; rows: Row[]; depth_symbols: string[] };
export function UniverseTable({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("trades");
  const [page, setPage] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const res = await fetch("/api/scanner/universe", { signal: AbortSignal.timeout(8000), cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const next = await res.json() as Data;
        if (!cancelled) { setData(next); setError(null); }
      } catch (e) { if (!cancelled) setError(String(e)); }
      if (!cancelled) timer = window.setTimeout(poll, 15000);
    }
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, []);
  const rows = (data?.rows ?? []).filter(r => r.symbol.includes(query.toUpperCase())).sort((a, b) =>
    sort === "thin" ? a.top_depth_usdt - b.top_depth_usdt : sort === "spread" ? (b.spread_pct ?? -1) - (a.spread_pct ?? -1) : b.trades - a.trades);
  const stale = !data?.updated_ns || Date.now() - data.updated_ns / 1e6 > 120000;
  return <section className="card">
    <header className="card__head"><h2>Весь рынок · {data?.rows.length ?? 0} USDT-пар</h2></header>
    <p>Все доступные Binance Spot USDT-пары. Полные стаканы и стратегии: {data?.depth_symbols.length ?? 0} пар.
      Обзор обновляется раз в минуту; торговый список отбирается при запуске сервера.</p>
    <p>Обновлено: {data?.updated_ns ? new Date(data.updated_ns / 1e6).toLocaleString("ru-RU") : "ожидание"}
      {stale ? " · данные устарели или ещё не получены" : ""}</p>
    {error || data?.error ? <p className="status__error">{error ?? data?.error}</p> : null}
    <label>Поиск монеты <input value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} /></label>{" "}
    <label>Сортировка <select value={sort} onChange={e => { setSort(e.target.value); setPage(0); }}>
      <option value="trades">Больше сделок</option><option value="spread">Шире спред</option><option value="thin">Меньше объём лучшей котировки</option>
    </select></label>
    <div style={{ overflowX: "auto" }}><table className="table"><thead><tr>
      <th>Пара</th><th>Цена</th><th>24ч %</th><th>Сделок 24ч</th><th>Оборот USDT</th><th>Спред %</th><th>Мин. BID/ASK, USDT</th><th>Полный стакан</th>
    </tr></thead><tbody>{rows.slice(page * 50, (page + 1) * 50).map(r => <tr key={r.symbol}>
      <td>{r.symbol}</td><td>{r.price}</td><td>{r.change_pct.toFixed(2)}</td><td>{r.trades.toLocaleString("ru-RU")}</td>
      <td>{r.volume_usdt.toFixed(0)}</td><td>{r.spread_pct?.toFixed(3) ?? "—"}</td><td>{r.top_depth_usdt.toFixed(2)}</td>
      <td>{r.monitored ? <button onClick={() => onSelect(r.symbol)}>Открыть</button> : "Только обзор"}</td>
    </tr>)}</tbody></table></div>
    <p>Объём лучшей котировки — только верхний уровень, не глубина всего стакана.</p>
    <button disabled={page === 0} onClick={() => setPage(page - 1)}>Назад</button>{" "}
    <span>{page + 1} / {Math.max(1, Math.ceil(rows.length / 50))}</span>{" "}
    <button disabled={(page + 1) * 50 >= rows.length} onClick={() => setPage(page + 1)}>Далее</button>
  </section>;
}

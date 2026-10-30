import { useEffect, useState } from "react";

type Strategy = { enabled: boolean; order_notional: string; min_edge_bps: string; min_spread_bps: string;
  min_obi: number; min_wall_score: number; min_trades: number; adverse_selection_bps: string };
type Order = { id: string; symbol: string; side: string; kind: string; status: string; quantity: string;
  filled: string; price: string; fees: string; reason: string; strategy: string };
type Position = { symbol: string; strategy: string; quantity: string; average_cost: string; unrealized_pnl: string;
  exit_reason: string | null; mark_stale: boolean; dust?: boolean };
type PaperStatus = { available?: boolean; mode: string; running: boolean; halt_reason: string | null;
  data_paused?: boolean; flattening: boolean; cash: string; free_cash: string; reserved_cash: string; equity: string; realized_pnl: string;
  fees: string; positions: Position[]; orders: Order[]; strategies: Record<string, Strategy>;
  decisions: { symbol: string; strategy: string; reason: string; allowed: boolean; edge_bps: string }[];
  settings?: { continuous?: boolean }; storage_error: string | null };
type Event = { sequence: number; kind: string; ts_ns: number; reason?: string; symbol?: string;
  quantity?: string; price?: string; fee?: string; side?: string; realized?: string; strategy?: string; order?: Order };
const ACTIVE = new Set(["SUBMITTING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING"]);
const NAMES: Record<string, string> = { spread_capture: "Сбор спреда", liquidity_wall: "Ликвидность стен", microstructure: "Микроструктура" };
const base = import.meta.env.VITE_API_BASE ?? "";
async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`${base}/api${path}`, { method, signal: AbortSignal.timeout(8000), headers: { "Content-Type": "application/json", "X-Paper-Command": "1" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data as T;
}
const money = (value: string) => Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 4 });

function StrategyEditor({ name, value, save }: { name: string; value: Strategy; save: (name: string, value: Strategy) => Promise<void> }) {
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(value), [value.enabled, value.order_notional, value.min_edge_bps, value.min_spread_bps,
    value.min_obi, value.min_wall_score, value.min_trades, value.adverse_selection_bps]);
  return <form className="paper-strategy" onSubmit={async e => { e.preventDefault(); setBusy(true); try { await save(name, draft); } finally { setBusy(false); } }}>
    <label className="paper-check"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />{NAMES[name] ?? name}</label>
    <label>Заявка, USDT<input type="number" min="1" step="1" value={draft.order_notional} onChange={e => setDraft({ ...draft, order_notional: e.target.value })} required /></label>
    <label>Мин. преимущество, bps<input type="number" min="0" step="0.1" value={draft.min_edge_bps} onChange={e => setDraft({ ...draft, min_edge_bps: e.target.value })} required /></label>
    <label>Мин. спред, bps<input type="number" min="0" step="0.1" value={draft.min_spread_bps} onChange={e => setDraft({ ...draft, min_spread_bps: e.target.value })} required /></label>
    <details><summary>Дополнительные параметры</summary>
      <label>OBI<input type="number" min="-1" max="1" step="0.05" value={draft.min_obi} onChange={e => setDraft({ ...draft, min_obi: Number(e.target.value) })} /></label>
      <label>Оценка стены<input type="number" min="0" max="100" value={draft.min_wall_score} onChange={e => setDraft({ ...draft, min_wall_score: Number(e.target.value) })} /></label>
      <label>Минимум сделок<input type="number" min="1" value={draft.min_trades} onChange={e => setDraft({ ...draft, min_trades: Number(e.target.value) })} /></label>
      <label>Резерв издержек, bps<input type="number" min="0" step="0.1" value={draft.adverse_selection_bps} onChange={e => setDraft({ ...draft, adverse_selection_bps: e.target.value })} /></label>
    </details>
    <button disabled={busy} type="submit">{busy ? "Сохранение…" : "Сохранить"}</button>
  </form>;
}

export function TradingPanel({ symbol }: { symbol: string | null }) {
  const [state, setState] = useState<PaperStatus | null>(null);
  const [fills, setFills] = useState<Event[]>([]);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [side, setSide] = useState("BUY");
  const [kind, setKind] = useState("POST_ONLY");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const status = await api<PaperStatus>("/paper/status");
        if (!cancelled) { setState(status); setUpdatedAt(Date.now()); setError(null); }
        if (status.available) {
          const [history, executions] = await Promise.allSettled([
            api<Event[]>("/paper/events?limit=30"), api<Event[]>("/trades?limit=100")
          ]);
          if (!cancelled) {
            if (history.status === "fulfilled") setEvents(history.value);
            if (executions.status === "fulfilled") setFills(executions.value);
            setHistoryError(history.status === "rejected" || executions.status === "rejected"
              ? "История временно недоступна; повторяем загрузку. Статус счёта получен отдельно." : null);
          }
        }
      } catch (e) { if (!cancelled) { setError(`Нет ответа от счёта. Повторяем подключение. ${String(e)}`); setNotice(null); } }
      if (!cancelled) timer = window.setTimeout(poll, 1000);
    }
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, []);
  async function action(path: string, method = "POST", body?: unknown) {
    setBusy(true); setError(null); setNotice(null);
    try { const result = await api<PaperStatus>(path, method, body); setState({ ...result, available: true });
      const last = result.orders.at(-1);
      setNotice(path === "/paper/orders" && last?.status === "REJECTED" ? `Заявка отклонена: ${last.reason}` : "Команда выполнена");
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  if (!state) return <section className="card"><h2>Торговая система</h2><p>{error ?? "Загрузка виртуального счёта…"}</p></section>;
  if (!state.available) return <section className="card"><h2>Торговая система</h2><p>Режим исследования. Для виртуальной торговли запустите профиль Paper Trading.</p></section>;
  const active = state.orders.filter(o => ACTIVE.has(o.status));
  return <section className="card paper-panel">
    <div className="paper-title"><div><h2>Торговая система <span className="tag">PAPER</span></h2><p>Реальные котировки · виртуальные USDT · реальные ордера отключены</p></div>
      <span className={`pill ${state.running && !error ? "pill--ok" : "pill--warn"}`}>{error ? "Нет связи" : state.running ? "Автоторговля работает" : state.flattening ? "Снижение позиции" : state.data_paused ? "Восстановление данных" : "На паузе"}</span></div>
    <p className="paper-note">Интерфейс 30.10-r7 · Обновлено: {updatedAt ? new Date(updatedAt).toLocaleString("ru-RU") : "ожидание"}.
      {error ? " Связь со счётом потеряна — показаны последние полученные данные." : ""}
      {state.data_paused ? " Автозапуск после 10 секунд устойчивых данных и проверки лимитов." : ""}</p>
    {state.settings?.continuous && <p className="paper-note">До команды «Стоп» · PAPER без остановки по времени, серии убытков и просадке счёта. Ограничения заявок и проверки данных действуют.</p>}
    {state.halt_reason && <p className="paper-note">{state.halt_reason}</p>}
    {(error || state.storage_error) && <p role="alert" className="status__error">{error ?? state.storage_error}</p>}
    {historyError && <p role="status" className="paper-note">{historyError}</p>}
    {notice && <p role="status" className="paper-note">{notice}</p>}
    <div className="paper-stats">{[["Капитал", state.equity], ["Свободно USDT", state.free_cash], ["В резерве", state.reserved_cash],
      ["Реализованный PnL", state.realized_pnl], ["Комиссии", state.fees]].map(([label, value]) => <div key={label}><small>{label}</small><strong>{money(value!)}</strong></div>)}</div>
    <p className="paper-note">Последнее исполнение: {fills[0]
      ? `${new Date(fills[0].ts_ns / 1e6).toLocaleString("ru-RU")} · ${fills[0].symbol} · ${fills[0].side}`
      : "ещё не было"}. Активных заявок: {active.length}.</p>
    <div className="paper-actions">
      <button disabled={busy || state.running || !!state.storage_error} onClick={() => void action("/paper/start")}>Запустить Paper</button>
      <button disabled={busy} onClick={() => void action("/paper/stop")}>Стоп + отмена заявок</button>
      <button className="paper-danger" disabled={busy} onClick={() => void action("/paper/flatten")}>Закрыть позиции в пределах лимита</button>
    </div>
    <p className="paper-note">Закрытие зависит от свежести стакана, ликвидности и биржевых минимумов. Остаток позиции остаётся видимым.</p>
    <div className="paper-strategies">{Object.entries(state.strategies).map(([name, value]) => <StrategyEditor key={name} name={name} value={value}
      save={(n, v) => action(`/strategies/${n}/config`, "PUT", v)} />)}</div>
    <h3>Решения стратегий</h3>
    <div className="paper-decisions">{state.decisions.length ? state.decisions.map(d => <p key={`${d.symbol}:${d.strategy}`}><b>{d.symbol} · {NAMES[d.strategy]}</b> — {d.reason}</p>) : <p>После запуска здесь появятся причины входа или ожидания.</p>}</div>
    <h3>Позиции</h3><div className="paper-scroll"><table><thead><tr><th>Пара</th><th>Количество</th><th>Стоимость входа</th><th>Нереализ. PnL</th><th>Причина выхода</th></tr></thead><tbody>
      {state.positions.map(p => <tr key={p.symbol}><td>{p.symbol}</td><td>{p.quantity}</td><td>{p.average_cost}</td><td>{money(p.unrealized_pnl)}{p.mark_stale ? " · старая оценка" : ""}</td><td>{p.dust ? "Мелкий остаток: ниже минимума продажи; не занимает место для новой позиции" : p.exit_reason ?? "Пассивная фиксация прибыли"}</td></tr>)}
      {!state.positions.length && <tr><td colSpan={5}>Открытых позиций нет</td></tr>}</tbody></table></div>
    <h3>Активные ордера</h3><div className="paper-scroll"><table><thead><tr><th>Пара</th><th>Сторона</th><th>Цена</th><th>Исполнено / объём</th><th>Статус</th><th /></tr></thead><tbody>
      {active.map(o => <tr key={o.id}><td>{o.symbol}</td><td>{o.side} · {o.kind}</td><td>{o.price}</td><td>{o.filled} / {o.quantity}</td><td>{o.status}</td><td><button disabled={busy} onClick={() => void action(`/paper/orders/${o.id}/cancel`)}>Отмена</button></td></tr>)}
      {!active.length && <tr><td colSpan={6}>Активных ордеров нет</td></tr>}</tbody></table></div>
    <details><summary>Ручная виртуальная заявка · {symbol ?? "пара не выбрана"}</summary>
      <form className="paper-manual" onSubmit={e => { e.preventDefault(); void action("/paper/orders", "POST", {symbol, side, kind, quantity, price, client_order_id: crypto.randomUUID()}); }}>
        <label>Сторона<select value={side} onChange={e => setSide(e.target.value)}><option>BUY</option><option>SELL</option></select></label>
        <label>Тип<select value={kind} onChange={e => setKind(e.target.value)}><option>POST_ONLY</option><option>IOC</option></select></label>
        <label>Количество<input value={quantity} onChange={e => setQuantity(e.target.value)} inputMode="decimal" required /></label>
        <label>Лимитная цена<input value={price} onChange={e => setPrice(e.target.value)} inputMode="decimal" required /></label>
        <button disabled={busy || !state.running || !symbol}>Создать виртуальную заявку</button>
      </form><p className="paper-note">Заявка проходит общие риск-лимиты. Неверные tick/step и недостаточный баланс приводят к отказу.</p>
    </details>
    <h3>Исполнения · последние 100</h3>
    <p className="paper-note">Покупки и продажи сохраняются здесь после закрытия позиции. Время — часовой пояс браузера.</p>
    <div className="paper-scroll"><table><thead><tr><th>Дата и время</th><th>Пара</th><th>Стратегия</th><th>Сторона</th><th>Количество</th><th>Цена</th><th>Комиссия</th><th>Реализованный PnL</th></tr></thead><tbody>
      {fills.map(f => <tr key={f.sequence}><td>{new Date(f.ts_ns / 1e6).toLocaleString("ru-RU")}</td><td>{f.symbol}</td><td>{NAMES[f.strategy ?? ""] ?? f.strategy}</td><td>{f.side}</td><td>{f.quantity}</td><td>{f.price}</td><td>{f.fee}</td><td>{f.side === "SELL" ? money(f.realized ?? "0") : "—"}</td></tr>)}
      {!fills.length && <tr><td colSpan={8}>В этом счёте исполнений ещё не было</td></tr>}
    </tbody></table></div>
    <h3>Журнал счёта</h3><div className="paper-log">{events.map(e => <div key={e.sequence}><time>{new Date(e.ts_ns / 1e6).toLocaleTimeString("ru-RU")}</time> <b>{e.kind}</b> {e.symbol ?? e.order?.symbol ?? ""} {e.reason ?? e.order?.reason ?? ""}{e.quantity ? ` · ${e.side} ${e.quantity} @ ${e.price}, комиссия ${e.fee}` : ""}</div>)}</div>
    <p className="paper-note">Исполнения оцениваются по L2 и публичным сделкам. Сигналы эвристические, прибыльность не подтверждена.</p>
  </section>;
}

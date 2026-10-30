import { useEffect, useMemo, useState } from "react";
import { fetchInstruments } from "./api";
import { MetricsPanel } from "./components/MetricsPanel";
import { OrderBook } from "./components/OrderBook";
import { UniverseTable } from "./components/UniverseTable";
import { ScannerTable } from "./components/ScannerTable";
import { StatusBar } from "./components/StatusBar";
import { TradingPanel } from "./components/TradingPanel";
import { WallsPanel } from "./components/WallsPanel";
import { decimalsForStep } from "./format";
import { useMarketStream } from "./hooks/useMarketStream";
import { useScannerResults, useSystemStatus } from "./hooks/usePolling";
import type { Instrument } from "./types";

function Dashboard() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentsError, setInstrumentsError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState<string | null>(null);

  const { data: status, error: statusError } = useSystemStatus();
  const { data: scannerRows } = useScannerResults();
  const stream = useMarketStream(symbol);

  useEffect(() => {
    let cancelled = false;
    let retry: number | undefined;
    const load = async () => {
      try {
        const items = await fetchInstruments();
        if (cancelled) return;
        setInstruments(items);
        setInstrumentsError(null);
        if (items.length) setSymbol(current => current ?? items[0]!.symbol);
      } catch (error) {
        if (cancelled) return;
        setInstrumentsError(`Список инструментов недоступен. Повтор через 3 секунды. ${error instanceof Error ? error.message : String(error)}`);
        retry = window.setTimeout(load, 3000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(retry);
    };
  }, []);

  // Если сервис перезапустился с другим списком символов — подстраиваемся.
  useEffect(() => {
    if (!status) {
      return;
    }
    setSymbol((current) => (current && status.symbols.includes(current) ? current : (status.symbols[0] ?? null)));
  }, [status]);

  const symbols = status?.symbols ?? instruments.map((item) => item.symbol);
  const decimals = useMemo(() => {
    const instrument = instruments.find((item) => item.symbol === symbol);
    return decimalsForStep(instrument?.tick_size);
  }, [instruments, symbol]);

  const data = stream.data;

  return (
    <div className="app">
      <StatusBar
        status={status}
        statusError={statusError ?? instrumentsError}
        streamStatus={stream.status}
        streamError={stream.error}
        lastUpdateAt={stream.lastUpdateAt}
        symbol={symbol}
        symbols={symbols}
        onSymbolChange={setSymbol}
      />

      {!data ? (
        <section className="card card--placeholder">
          <h2>Ожидание данных</h2>
          <p>
            {symbol
              ? `Подключаемся к потоку ${symbol}. Пока стакан не подтвердил целостность, торговые признаки не считаются.`
              : "Сервер не сообщил список отслеживаемых инструментов."}
          </p>
          {stream.error ? <p className="status__error">{stream.error}</p> : null}
        </section>
      ) : (
        <main className="layout">
          <div className="layout__left">
            <OrderBook key={symbol} tickSize={instruments.find(item => item.symbol === symbol)?.tick_size ?? "0.01"} bids={data.bids} asks={data.asks} decimals={decimals} walls={data.walls} />
            <WallsPanel walls={data.walls} decimals={decimals} />
          </div>
          <div className="layout__right">
            <MetricsPanel data={data} />
          </div>
        </main>
      )}

      <TradingPanel symbol={symbol} />

      <UniverseTable onSelect={setSymbol} />

      <ScannerTable
        rows={scannerRows ?? []}
        selectedSymbol={symbol}
        onSelect={(value) => setSymbol(value)}
      />

      <footer className="footer">
        <span>
          Рыночные данные, исследования и Paper Trading. Live trading отключён.
        </span>
        <span>
          Метки: <span className="tag tag--verified">verified</span> — данные биржи,{" "}
          <span className="tag tag--estimated">оценка</span> — вывод из агрегированного L2.
        </span>
      </footer>
    </div>
  );
}


export default function App() {
  const [tab, setTab] = useState("main");
  const [opened, setOpened] = useState(false);
  const experimental = window.location.port === "8002";
  if (experimental) return <><div className="experiment-note"><b>Низкая ликвидность · отдельный Paper-счёт</b><p>Начальный бюджет 100 USDT · заявка 10 USDT · несколько позиций в пределах общей экспозиции 50 USDT. Средства и журнал отделены от основного теста.</p></div><Dashboard /></>;
  return <>
    <nav className="experiment-tabs" aria-label="Торговые эксперименты">
      <button type="button" aria-pressed={tab === "main"} onClick={() => setTab("main")}>Основной рынок</button>
      <button type="button" aria-pressed={tab === "low"} onClick={() => { setOpened(true); setTab("low"); }}>Низкая ликвидность · 100 USDT</button>
    </nav>
    <div hidden={tab !== "main"}><Dashboard /></div>
    <div hidden={tab !== "low"} className="experiment-pane">
      <p>Независимый эксперимент. Переключение вкладок не останавливает торговлю. <a href="http://127.0.0.1:8002/" target="_blank" rel="noreferrer">Открыть отдельно</a></p>
      {opened && <iframe title="Низкая ликвидность — отдельный торговый счёт" src="http://127.0.0.1:8002/" />}
    </div>
  </>;
}

import { fmtDuration, fmtPct, fmtQty, fmtSigned, toNumber } from "../format";
import type { MarketPayload } from "../types";

interface MetricsPanelProps {
  data: MarketPayload;
}

function ImbalanceBar({ label, value }: { label: string; value: number | null | undefined }) {
  const numeric = value ?? null;
  const clamped = numeric === null ? 0 : Math.max(-1, Math.min(1, numeric));
  const width = Math.abs(clamped) * 50;
  const positive = clamped >= 0;
  return (
    <div className="metric-row">
      <span className="metric-row__label">{label}</span>
      <div className="meter" aria-hidden="true">
        <div className="meter__mid" />
        <div
          className={`meter__fill meter__fill--${positive ? "bid" : "ask"}`}
          style={positive ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }}
        />
      </div>
      <span className={`metric-row__value metric-row__value--${positive ? "bid" : "ask"}`}>
        {numeric === null ? "UNDEFINED" : fmtSigned(numeric, 3)}
      </span>
    </div>
  );
}

export function MetricsPanel({ data }: MetricsPanelProps) {
  const spread = data.spread;
  const obiEntries = ["1", "5", "10", "20", "50"].map((key) => ({
    key,
    value: data.obi[key] ?? null,
  }));

  const buyVolume = toNumber(data.flow.buy_volume) ?? 0;
  const sellVolume = toNumber(data.flow.sell_volume) ?? 0;
  const flowTotal = buyVolume + sellVolume;
  const buyShare = flowTotal > 0 ? (buyVolume / flowTotal) * 100 : 50;

  return (
    <section className="card">
      <header className="card__head">
        <h2>Метрики</h2>
        <span className={`badge badge--${data.tradable ? "ok" : "warn"}`}>
          {data.tradable ? "tradable" : `заблокировано: ${data.block_reason ?? data.book_state}`}
        </span>
      </header>

      <div className="meters">
        <h3>Спред</h3>
        <dl className="kv">
          <div>
            <dt>значение</dt>
            <dd>{spread.value === null ? "—" : spread.value.toFixed(6)}</dd>
          </div>
          <div>
            <dt>%</dt>
            <dd>{fmtPct(spread.pct)}</dd>
          </div>
          <div>
            <dt>bps</dt>
            <dd>{spread.bps === null ? "—" : spread.bps.toFixed(2)}</dd>
          </div>
          <div>
            <dt>тиков</dt>
            <dd>{spread.ticks === null ? "—" : spread.ticks.toFixed(2)}</dd>
          </div>
          <div>
            <dt>медиана окна</dt>
            <dd>{spread.median === null ? "—" : spread.median.toFixed(6)}</dd>
          </div>
          <div>
            <dt>перцентиль</dt>
            <dd>{spread.percentile === null ? "—" : `${(spread.percentile * 100).toFixed(0)}%`}</dd>
          </div>
        </dl>

        <h3>Дисбаланс стакана (OBI)</h3>
        {obiEntries.map((entry) => (
          <ImbalanceBar key={entry.key} label={`N=${entry.key}`} value={entry.value} />
        ))}
        <ImbalanceBar label="взвеш. по расстоянию" value={data.obi_weighted} />

        <h3>
          Microprice <span className="tag tag--estimated">оценка</span>
        </h3>
        <dl className="kv">
          <div>
            <dt>значение</dt>
            <dd>{data.microprice.value === null ? "—" : data.microprice.value.toFixed(6)}</dd>
          </div>
          <div>
            <dt>отклонение от mid</dt>
            <dd>{data.microprice.dev_bps === null ? "—" : `${fmtSigned(data.microprice.dev_bps, 3)} bps`}</dd>
          </div>
          <div>
            <dt>скорость</dt>
            <dd>
              {data.microprice.velocity_bps_s === null
                ? "—"
                : `${fmtSigned(data.microprice.velocity_bps_s, 3)} bps/с`}
            </dd>
          </div>
        </dl>

        <h3>Поток ордеров (OFI)</h3>
        <dl className="kv">
          <div>
            <dt>top-of-book</dt>
            <dd>{fmtSigned(data.ofi.top, 3)}</dd>
          </div>
          <div>
            <dt>multi-level</dt>
            <dd>{fmtSigned(data.ofi.multi, 3)}</dd>
          </div>
          <div>
            <dt>rolling</dt>
            <dd>{fmtSigned(data.ofi.rolling, 3)}</dd>
          </div>
        </dl>

        <h3>
          Агрессивный поток <span className="tag tag--verified">по полям биржи</span>
        </h3>
        <div className="flow-bar" aria-hidden="true">
          <div className="flow-bar__buy" style={{ width: `${buyShare}%` }} />
          <div className="flow-bar__sell" style={{ width: `${100 - buyShare}%` }} />
        </div>
        <dl className="kv">
          <div>
            <dt>покупки</dt>
            <dd className="value-bid">{fmtQty(data.flow.buy_volume)}</dd>
          </div>
          <div>
            <dt>продажи</dt>
            <dd className="value-ask">{fmtQty(data.flow.sell_volume)}</dd>
          </div>
          <div>
            <dt>CVD</dt>
            <dd>{fmtSigned(toNumber(data.flow.cvd), 4)}</dd>
          </div>
          <div>
            <dt>сделок в окне</dt>
            <dd>{data.flow.trades}</dd>
          </div>
          <div>
            <dt>классифицировано</dt>
            <dd>{fmtPct(data.flow.classified_share * 100, 1)}</dd>
          </div>
        </dl>

        <h3>Данные</h3>
        <dl className="kv">
          <div>
            <dt>состояние стакана</dt>
            <dd>{data.book_state}</dd>
          </div>
          <div>
            <dt>возраст стакана</dt>
            <dd>{fmtDuration(data.book_age_ms)}</dd>
          </div>
          <div>
            <dt>последний exchange ts</dt>
            <dd>
              {data.latency_ms.last_exchange_ts_ms === null
                ? "—"
                : new Date(data.latency_ms.last_exchange_ts_ms).toISOString().slice(11, 23)}
            </dd>
          </div>
          <div>
            <dt>снимок признаков</dt>
            <dd>
              {data.server_ts_ms
                ? new Date(data.server_ts_ms).toISOString().slice(11, 23)
                : "—"}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

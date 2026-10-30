"""Three causal, explicitly heuristic research signals; no trained probability claims."""

from decimal import Decimal

from app.paper.config import PaperSettings, StrategySettings
from app.paper.models import Decision, Market

D = Decimal


def evaluate(name: str, config: StrategySettings, paper: PaperSettings, market: Market) -> Decision:
    def no(reason: str) -> Decision:
        return Decision(name, market.symbol, False, reason)

    if not config.enabled:
        return no("Стратегия выключена")
    if not market.ready:
        return no("Стакан не готов")
    if market.trades < config.min_trades:
        return no("Недостаточно публичных сделок в окне")
    flow = market.buy_volume + market.sell_volume
    pressure = (market.buy_volume - market.sell_volume) / flow if flow else D(0)
    if pressure < D("-0.3") or market.obi < -0.5:
        return no("Токсичный поток: преобладают продажи")
    gross = market.spread_bps
    if name == "spread_capture":
        if gross < config.min_spread_bps:
            return no("Спред меньше порога")
    elif name == "liquidity_wall":
        if (
            not market.wall_persistent
            or market.wall_score < config.min_wall_score
            or market.wall_absorbed <= 0
            or market.wall_price is None
        ):
            return no("Нет устойчивой BID-стены с поглощением")
        if abs(market.bid - market.wall_price) / market.bid > D("0.002"):
            return no("BID-стена слишком далеко")
        if market.obi < config.min_obi or market.ofi <= 0:
            return no("Стена не подтверждена OBI/OFI")
    elif name == "microstructure":
        if market.obi < config.min_obi or market.ofi <= 0 or market.microprice_bps <= 0 or pressure <= 0:
            return no("Нет согласованного OBI/OFI/microprice/потока")
        # microprice is a directional filter, not a calibrated forecast in bps.
        # Execution is passive bid -> ask; gross spread is only its upper bound.
        gross = market.spread_bps
    else:
        return no("Неизвестная стратегия")
    # Heuristic cost screen only. Include measured sell pressure in the adverse-selection reserve.
    adverse = config.adverse_selection_bps * (1 + max(D(0), -pressure))
    cost = paper.maker_fee_bps + paper.taker_fee_bps + adverse
    edge = gross - cost
    return Decision(
        name,
        market.symbol,
        edge > config.min_edge_bps,
        "Условия исследовательского входа выполнены"
        if edge > config.min_edge_bps
        else "Преимущество не покрывает комиссии и резерв издержек",
        edge,
        {
            "gross_bps": str(gross),
            "cost_bps": str(cost),
            "trade_pressure": str(pressure),
            "model": "passive_spread_ceiling_not_forecast",
        },
    )

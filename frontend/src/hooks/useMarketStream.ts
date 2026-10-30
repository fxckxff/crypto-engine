import { useEffect, useRef, useState } from "react";
import { marketSocketUrl } from "../api";
import type { MarketPayload, StreamStatus } from "../types";

interface MarketStreamState {
  data: MarketPayload | null;
  status: StreamStatus;
  lastUpdateAt: number | null;
  error: string | null;
}

const RECONNECT_MAX_MS = 10_000;

/**
 * Подписка на /ws/market/{symbol}.
 *
 * Данные приходят двумя типами сообщений: snapshot (первое состояние) и update.
 * UI не пересчитывает признаки — он показывает то, что посчитал движок,
 * поэтому частота обновлений здесь не влияет на торговую логику.
 */
export function useMarketStream(symbol: string | null): MarketStreamState {
  const [state, setState] = useState<MarketStreamState>({
    data: null,
    status: "connecting",
    lastUpdateAt: null,
    error: null,
  });
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!symbol) {
      return undefined;
    }

    let cancelled = false;
    let attempt = 0;
    let timer: number | undefined;

    const connect = () => {
      if (cancelled) {
        return;
      }
      setState((prev) => ({
        ...prev,
        status: attempt === 0 ? "connecting" : "reconnecting",
        data: prev.data && prev.data.symbol === symbol ? prev.data : null,
      }));

      const socket = new WebSocket(marketSocketUrl(symbol));
      socketRef.current = socket;

      socket.onopen = () => {
        if (cancelled) return;
        attempt = 0;
        setState((prev) => ({ ...prev, status: "open", error: null }));
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (cancelled) return;
        let message: { type?: string; data?: MarketPayload };
        try {
          message = JSON.parse(event.data) as { type?: string; data?: MarketPayload };
        } catch {
          return;
        }
        if (message.type === "ping" || !message.data) {
          return;
        }
        setState({
          data: message.data,
          status: "open",
          lastUpdateAt: Date.now(),
          error: null,
        });
      };

      socket.onerror = () => {
        if (cancelled) return;
        // Детали ошибки браузер не отдаёт: close придёт следом и запустит переподключение.
        setState((prev) => ({ ...prev, error: "ошибка WebSocket" }));
      };

      socket.onclose = (event: CloseEvent) => {
        if (socketRef.current === socket) socketRef.current = null;
        if (cancelled) {
          return;
        }
        const reason =
          event.code === 4004
            ? `символ ${symbol} не мониторится сервером`
            : `соединение закрыто (код ${event.code})`;
        setState((prev) => ({ ...prev, status: "reconnecting", error: reason }));
        if (event.code === 4004) {
          return; // переподключение не поможет: символ не в конфиге
        }
        attempt += 1;
        const delay = Math.min(500 * 2 ** (attempt - 1), RECONNECT_MAX_MS);
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && socket.readyState <= WebSocket.OPEN) {
        socket.close(1000, "symbol changed");
      }
      setState({ data: null, status: "connecting", lastUpdateAt: null, error: null });
    };
  }, [symbol]);

  return state;
}

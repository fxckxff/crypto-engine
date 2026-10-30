import { useEffect, useState } from "react";
import { fetchScanner, fetchStatus } from "../api";
import type { ScannerPayload, SystemStatus } from "../types";

interface PollingState<T> {
  data: T | null;
  error: string | null;
}

export function useSystemStatus(intervalMs = 3000): PollingState<SystemStatus> {
  const [state, setState] = useState<PollingState<SystemStatus>>({ data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const status = await fetchStatus();
        if (!cancelled) {
          setState({ data: status, error: null });
        }
      } catch (error) {
        if (!cancelled) {
          setState((prev) => ({ ...prev, error: error instanceof Error ? error.message : String(error) }));
        }
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(poll, intervalMs);
        }
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [intervalMs]);

  return state;
}

export function useScannerResults(intervalMs = 2000): PollingState<ScannerPayload[]> {
  const [state, setState] = useState<PollingState<ScannerPayload[]>>({ data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const rows = await fetchScanner();
        if (!cancelled) {
          setState({ data: rows, error: null });
        }
      } catch (error) {
        if (!cancelled) {
          setState((prev) => ({ ...prev, error: error instanceof Error ? error.message : String(error) }));
        }
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(poll, intervalMs);
        }
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [intervalMs]);

  return state;
}

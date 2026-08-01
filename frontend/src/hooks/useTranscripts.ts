import { useCallback, useEffect, useRef, useState } from "react";
import type { Turn } from "../types";

/* Conversations survive a reload. Keyed by user for now; Phase 1 re-keys these
   by session id so two tabs stop sharing one thread.

   `pending` turns are never persisted — a reload mid-request would otherwise
   restore a skeleton that no response is coming for. */

const KEY = "ledger.transcripts.v1";

type Store = Record<string, Turn[]>;

function load(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Store;
  } catch {
    return {};
  }
}

export function useTranscripts() {
  const [store, setStore] = useState<Store>(load);

  // Debounced so a burst of turns doesn't serialize the whole store repeatedly.
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      try {
        const durable: Store = {};
        for (const [key, turns] of Object.entries(store)) {
          const keep = turns.filter((t) => t.role !== "pending");
          if (keep.length) durable[key] = keep;
        }
        localStorage.setItem(KEY, JSON.stringify(durable));
      } catch {
        /* quota or private mode — the in-memory thread still works */
      }
    }, 300);
    return () => window.clearTimeout(timer.current);
  }, [store]);

  const turnsFor = useCallback((key: string): Turn[] => store[key] ?? [], [store]);

  const append = useCallback((key: string, ...turns: Turn[]) => {
    setStore((current) => ({ ...current, [key]: [...(current[key] ?? []), ...turns] }));
  }, []);

  /** Swap the in-flight skeleton for whatever actually arrived. */
  const resolvePending = useCallback((key: string, turn: Turn) => {
    setStore((current) => {
      const turns = current[key] ?? [];
      const index = turns.findIndex((t) => t.role === "pending");
      if (index < 0) return { ...current, [key]: [...turns, turn] };
      const next = turns.slice();
      next[index] = turn;
      return { ...current, [key]: next };
    });
  }, []);

  /** Drop the skeleton with no replacement — used when the user cancels. */
  const dropPending = useCallback((key: string) => {
    setStore((current) => ({
      ...current,
      [key]: (current[key] ?? []).filter((t) => t.role !== "pending"),
    }));
  }, []);

  const clear = useCallback((key: string) => {
    setStore((current) => ({ ...current, [key]: [] }));
  }, []);

  return { turnsFor, append, resolvePending, dropPending, clear };
}

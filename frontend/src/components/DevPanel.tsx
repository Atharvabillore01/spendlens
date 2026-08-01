import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { money } from "../lib/format";
import styles from "./DevPanel.module.css";

interface Props {
  userId: string | null;
  /** Bumped by the app after every turn so the snapshot re-reads. */
  epoch: number;
}

interface ProfileValue {
  user_name: string;
  date_range: [string, string];
  transaction_count: number;
  avg_monthly_spend: number;
  avg_monthly_income: number;
  top_categories: [string, number][];
}

interface HistoryEntry {
  prompt: string;
  result_summary?: string;
}

interface VizState {
  chart_type?: string;
  period?: string;
  filters?: Record<string, unknown>;
}

export default function DevPanel({ userId, epoch }: Props) {
  const [snapshot, setSnapshot] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!userId) return;
    try {
      setSnapshot(await api.cacheSnapshot(userId));
      setError(null);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, [userId]);

  useEffect(() => {
    void refresh();
  }, [refresh, epoch]);

  async function invalidate() {
    if (!userId) return;
    await api.invalidateCache(userId);
    await refresh();
  }

  return (
    <aside className={`${styles.inspector} devOnly`} aria-label="Developer panel">
      <div className={styles.head}>
        <p className={styles.sectionLabel}>KV cache</p>
        <button
          type="button"
          className={styles.ghostBtn}
          onClick={invalidate}
          title="Delete all three keys for this user"
        >
          invalidate
        </button>
      </div>
      <p className={styles.note}>
        The three per-user entries. Empty on load, populated after the first turn — and
        untouched when a guardrail blocks the prompt.
      </p>

      <div className={styles.panel}>
        {error && <p className={styles.empty}>Could not read cache: {error}</p>}
        {snapshot &&
          Object.entries(snapshot).map(([key, value]) => (
            <CacheEntry key={key} entryKey={key} value={value} />
          ))}
      </div>
    </aside>
  );
}

function CacheEntry({ entryKey, value }: { entryKey: string; value: unknown }) {
  const [open, setOpen] = useState(true);
  const empty = value == null || (Array.isArray(value) && value.length === 0);

  return (
    <div className={`${styles.entry} ${empty ? "" : styles.filled}`}>
      <button type="button" className={styles.entryKey} onClick={() => setOpen((v) => !v)}>
        <span>{entryKey}</span>
        <span className={`${styles.state} ${empty ? styles.unset : styles.set}`}>
          {empty ? "empty" : "set"}
        </span>
      </button>
      {open && (
        <div className={styles.entryBody}>
          {empty ? (
            <p className={styles.empty}>Not populated yet.</p>
          ) : entryKey.endsWith(":profile") ? (
            <ProfileBody value={value as ProfileValue} />
          ) : entryKey.endsWith(":query_history") ? (
            <HistoryBody entries={value as HistoryEntry[]} />
          ) : (
            <VizBody value={value as VizState} />
          )}
        </div>
      )}
    </div>
  );
}

function ProfileBody({ value }: { value: ProfileValue }) {
  const top = value.top_categories?.[0];
  return (
    <Definitions
      pairs={[
        ["name", value.user_name],
        ["range", value.date_range?.join(" → ") ?? "—"],
        ["txns", String(value.transaction_count)],
        ["avg spend/mo", money(value.avg_monthly_spend)],
        ["avg income/mo", money(value.avg_monthly_income)],
        ["top category", top ? `${top[0]} ${money(top[1])}` : "—"],
      ]}
    />
  );
}

function HistoryBody({ entries }: { entries: HistoryEntry[] }) {
  return (
    <>
      <ol className={styles.history}>
        {entries.map((entry, index) => (
          <li key={index}>
            <div>&ldquo;{entry.prompt}&rdquo;</div>
            {entry.result_summary && <div className={styles.empty}>→ {entry.result_summary}</div>}
          </li>
        ))}
      </ol>
      <p className={styles.empty}>{entries.length} entries · injected as few-shot examples</p>
    </>
  );
}

function VizBody({ value }: { value: VizState }) {
  return (
    <Definitions
      pairs={[
        ["chart", String(value.chart_type ?? "—")],
        ["resolved", String(value.period ?? "—")],
        ...Object.entries(value.filters ?? {}).map(
          ([key, val]) => [`arg ${key}`, String(val)] as [string, string],
        ),
      ]}
    />
  );
}

function Definitions({ pairs }: { pairs: [string, string][] }) {
  return (
    <dl className={styles.definitions}>
      {pairs.map(([term, value]) => (
        <div key={term} className={styles.defRow}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

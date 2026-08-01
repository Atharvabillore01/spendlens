import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ManagerRequest } from "../api/client";
import ChartCard from "./ChartCard";
import StatTiles from "./StatTiles";
import styles from "./Inbox.module.css";
import { CHART_TOOLS, type ChartTool, type QueryResult, type Theme } from "../types";

interface Props {
  /** Managers can run and reply; users see their own questions read-only. */
  canReply: boolean;
  theme: Theme;
  onCountsChanged: () => void;
}

export default function Inbox({ canReply, theme, onCountsChanged }: Props) {
  const [items, setItems] = useState<ManagerRequest[]>([]);
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listRequests();
      setItems(data.requests);
      setError(null);
    } catch (cause) {
      setError((cause as ApiError).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function submit() {
    const text = question.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      await api.submitRequest(text);
      setQuestion("");
      await refresh();
      onCountsChanged();
    } catch (cause) {
      setError((cause as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1>{canReply ? "Questions from your team" : "Ask your manager"}</h1>
        <p>
          {canReply
            ? "Run a question against that person's own data, then answer in your own words."
            : "Anything the assistant couldn't settle — a category that looks wrong, a figure you don't recognise."}
        </p>
      </header>

      {!canReply && (
        <section className={styles.composer}>
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. Why is my rent split across two categories?"
            rows={3}
            maxLength={2000}
          />
          <div className={styles.composerFoot}>
            <span className={styles.muted}>{question.length}/2000</span>
            <button
              type="button"
              className={styles.primary}
              disabled={!question.trim() || busy}
              onClick={submit}
            >
              {busy ? "Sending…" : "Send to manager"}
            </button>
          </div>
        </section>
      )}

      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      {items.length === 0 ? (
        <p className={styles.empty}>
          {canReply ? "No questions yet." : "You haven't asked anything yet."}
        </p>
      ) : (
        <ul className={styles.list}>
          {items.map((item) => (
            <RequestCard
              key={item.request_id}
              item={item}
              canReply={canReply}
              theme={theme}
              onChanged={async () => {
                await refresh();
                onCountsChanged();
              }}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function RequestCard({
  item,
  canReply,
  theme,
  onChanged,
}: {
  item: ManagerRequest;
  canReply: boolean;
  theme: Theme;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [ran, setRan] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState<"run" | "reply" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy("run");
    setError(null);
    try {
      const response = await api.runRequest(item.request_id, theme);
      setRan(response.result);
      // Seed the reply with what the data actually says, so the manager edits
      // a grounded answer rather than writing one from memory.
      if (!draft) setDraft(response.result.response);
      onChanged();
    } catch (cause) {
      setError((cause as ApiError).message);
    } finally {
      setBusy(null);
    }
  }

  async function send() {
    if (!draft.trim()) return;
    setBusy("reply");
    setError(null);
    try {
      await api.replyToRequest(item.request_id, draft.trim());
      onChanged();
    } catch (cause) {
      setError((cause as ApiError).message);
    } finally {
      setBusy(null);
    }
  }

  const summary = ran?.data_summary ?? item.computed_summary ?? null;
  const answer = ran?.response ?? item.computed_answer;

  return (
    <li className={`${styles.card} ${styles[item.status]}`}>
      <div className={styles.cardHead}>
        <div>
          {canReply && <span className={styles.who}>{item.from_user_name}</span>}
          <span className={styles.when}>{formatWhen(item.created_at)}</span>
        </div>
        <span className={`${styles.badge} ${styles[`badge_${item.status}`]}`}>{item.status}</span>
      </div>

      <p className={styles.question}>{item.question}</p>

      {answer && (
        <div className={styles.computed}>
          <span className={styles.computedLabel}>
            From {canReply ? `${item.from_user_name.split(" ")[0]}'s` : "your"} data
          </span>
          <p>{answer}</p>
          {summary && <StatTiles summary={summary} />}
          {summary &&
            CHART_TOOLS.filter((t) => summary[t] && !summary[t]?.no_data).map((tool) => (
              <ChartCard key={tool} tool={tool as ChartTool} summary={summary} />
            ))}
        </div>
      )}

      {item.reply && (
        <div className={styles.reply}>
          <span className={styles.replyLabel}>Reply</span>
          <p>{item.reply}</p>
        </div>
      )}

      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      {canReply && item.status !== "closed" && (
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={run} disabled={busy !== null}>
            {busy === "run" ? "Running…" : answer ? "Run again" : "Run against their data"}
          </button>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Write your answer…"
            rows={3}
          />
          <button
            type="button"
            className={styles.primary}
            onClick={send}
            disabled={!draft.trim() || busy !== null}
          >
            {busy === "reply" ? "Sending…" : item.reply ? "Update reply" : "Send reply"}
          </button>
        </div>
      )}
    </li>
  );
}

function formatWhen(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

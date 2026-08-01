import { useState } from "react";
import { FLAGS } from "../lib/flags";
import styles from "./MetaStrip.module.css";
import type { QueryResult } from "../types";

/* Two audiences, two strips.
   - Product: only what a user needs to know — that an answer was refused, or
     that it was produced without the model. Silence otherwise.
   - Developer: cache outcome, latency, model, every flag code, raw JSON. */
export default function MetaStrip({ result }: { result: QueryResult }) {
  const [showRaw, setShowRaw] = useState(false);
  const flags = result.guardrail_flags ?? [];
  const notice = userFacingNotice(result, flags);

  return (
    <>
      {notice && <p className={styles.notice}>{notice}</p>}

      <div className={`${styles.meta} devOnly`}>
        <span
          className={`${styles.pill} ${result.cache_hit ? styles.hit : styles.miss}`}
          title="cache_hit: whether the user profile was served from cache this turn"
        >
          {result.cache_hit ? "cache HIT" : "cache MISS"}
        </span>
        <span className={styles.pill}>{result.latency_ms} ms</span>
        {result.model_used ? (
          <span className={`${styles.pill} ${styles.model}`} title={result.model_used}>
            {result.model_used.split("/").pop()}
          </span>
        ) : (
          <span className={styles.pill} title="Blocked before the model — costs nothing">
            no LLM call
          </span>
        )}
        {result.degraded && <span className={`${styles.pill} ${styles.flag}`}>degraded</span>}
        {flags.map((flag) => {
          const info = FLAGS[flag] ?? { label: flag };
          return (
            <span
              key={flag}
              className={`${styles.pill} ${info.danger ? styles.flagDanger : styles.flag}`}
              title={flag}
            >
              {info.label}
            </span>
          );
        })}
        <button type="button" className={styles.toggle} onClick={() => setShowRaw((v) => !v)}>
          {showRaw ? "{ } hide" : "{ } raw"}
        </button>
      </div>

      {showRaw && (
        <pre className={`${styles.json} devOnly`}>{JSON.stringify(result, null, 2)}</pre>
      )}
    </>
  );
}

/** What an ordinary user is entitled to know, in plain language. */
function userFacingNotice(result: QueryResult, flags: string[]): string | null {
  if (flags.some((flag) => FLAGS[flag]?.danger)) return null; // the refusal text says it already
  if (result.degraded) {
    return "The assistant is unreachable right now, so this was answered directly from your transactions.";
  }
  if (flags.includes("hallucination_corrected")) {
    return "Some figures were corrected against your actual transactions.";
  }
  if (flags.includes("no_data_for_query")) return null; // the response explains it
  return null;
}

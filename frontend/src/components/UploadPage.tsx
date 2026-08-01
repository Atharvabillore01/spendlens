import { useCallback, useRef, useState } from "react";
import { api, ApiError, type IngestSummary, type PasteResponse } from "../api/client";
import styles from "./UploadPage.module.css";

type Mode = "paste" | "file";

const REQUIRED = [
  "user_id",
  "user_name",
  "transaction_date",
  "transaction_amount",
  "transaction_category_detail",
];
const OPTIONAL = ["merchant_name"];

export default function UploadPage({ onImported }: { onImported: () => void }) {
  const [mode, setMode] = useState<Mode>("paste");

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1>Add transactions</h1>
        <p>
          Paste straight from a spreadsheet, or upload the file. Either way you see exactly
          what will be imported before anything is saved.
        </p>
      </header>

      <div className={styles.tabs} role="tablist" aria-label="Import method">
        <button role="tab" aria-selected={mode === "paste"} onClick={() => setMode("paste")}>
          Paste from Excel
        </button>
        <button role="tab" aria-selected={mode === "file"} onClick={() => setMode("file")}>
          Upload a file
        </button>
      </div>

      {mode === "paste" ? <PastePanel onImported={onImported} /> : <FilePanel onImported={onImported} />}

      <section className={styles.schema}>
        <h2>Columns we look for</h2>
        <p>
          Header names are matched loosely — <code>Txn Date</code>, <code>Amount (USD)</code> and{" "}
          <code>Payee</code> are all understood. If a column is mis-read you can correct it
          below the preview.
        </p>
        <ul>
          {REQUIRED.map((c) => (
            <li key={c}>
              <code>{c}</code> <span className={styles.req}>required</span>
            </li>
          ))}
          {OPTIONAL.map((c) => (
            <li key={c}>
              <code>{c}</code> <span className={styles.opt}>optional</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/* ── paste ───────────────────────────────────────────────────────── */

function PastePanel({ onImported }: { onImported: () => void }) {
  const [text, setText] = useState("");
  const [result, setResult] = useState<PasteResponse | null>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<IngestSummary | null>(null);

  const preview = useCallback(
    async (value: string, columnOverrides: Record<string, string>) => {
      if (!value.trim()) {
        setResult(null);
        return;
      }
      setBusy(true);
      setError(null);
      try {
        setResult(
          await api.paste({ text: value, commit: false, column_overrides: columnOverrides }),
        );
      } catch (cause) {
        setResult(null);
        setError((cause as ApiError).message);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  async function commit() {
    setBusy(true);
    setError(null);
    try {
      const response = await api.paste({ text, commit: true, column_overrides: overrides });
      if (response.ingest) {
        setDone(response.ingest);
        setText("");
        setResult(null);
        setOverrides({});
        onImported();
      }
    } catch (cause) {
      setError((cause as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  function correct(column: string, target: string) {
    const next = { ...overrides };
    if (target) next[column] = target;
    else delete next[column];
    setOverrides(next);
    void preview(text, next);
  }

  if (done) return <Imported summary={done} onAgain={() => setDone(null)} />;

  const parse = result?.parse;

  return (
    <section className={styles.panel}>
      <label className={styles.pasteLabel} htmlFor="paste-area">
        Select the rows in Excel, copy, and paste here
      </label>
      <textarea
        id="paste-area"
        className={styles.paste}
        value={text}
        spellCheck={false}
        placeholder={
          "user_id\tuser_name\tdate\tamount\tcategory\tmerchant\n" +
          "usr_a1b2c3d4\tJose BazBaz\t31/12/2025\t$1,850.00\tRENT_HOUSING\tAvalonBay"
        }
        onChange={(event) => {
          setText(event.target.value);
          setDone(null);
        }}
        // Previewing on blur rather than on every keystroke: a paste of a few
        // thousand rows should not fire a request per character.
        onBlur={() => void preview(text, overrides)}
        onPaste={(event) => {
          const pasted = event.clipboardData.getData("text");
          if (pasted) {
            event.preventDefault();
            setText(pasted);
            void preview(pasted, overrides);
          }
        }}
      />

      {busy && <p className={styles.muted}>Reading…</p>}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}

      {parse && (
        <>
          <div className={styles.stats}>
            <Stat label="Rows" value={String(parse.rows_parsed)} />
            <Stat label="Header" value={parse.header_used ? "detected" : "none"} />
            <Stat
              label="Blank rows dropped"
              value={String(parse.dropped_blank_rows)}
              muted={parse.dropped_blank_rows === 0}
            />
          </div>

          {parse.missing_required.length > 0 && (
            <p className={styles.error} role="alert">
              Missing required column{parse.missing_required.length > 1 ? "s" : ""}:{" "}
              {parse.missing_required.join(", ")}. Map them below or re-copy including the header
              row.
            </p>
          )}

          {parse.coercion_notes.map((note) => (
            <p key={note} className={styles.warn}>
              {note}
            </p>
          ))}

          <ColumnMap parse={parse} overrides={overrides} onCorrect={correct} />

          {result.preview_rows.length > 0 && <PreviewTable rows={result.preview_rows} />}

          <button
            type="button"
            className={styles.primary}
            disabled={!parse.ok || busy}
            onClick={commit}
          >
            {parse.ok ? `Import ${parse.rows_parsed} row${parse.rows_parsed === 1 ? "" : "s"}` : "Fix the columns first"}
          </button>
        </>
      )}
    </section>
  );
}

function ColumnMap({
  parse,
  overrides,
  onCorrect,
}: {
  parse: PasteResponse["parse"];
  overrides: Record<string, string>;
  onCorrect: (column: string, target: string) => void;
}) {
  const targets = ["", ...REQUIRED, ...OPTIONAL];
  return (
    <div className={styles.mapping}>
      <h3>Column mapping</h3>
      <div className={styles.mapGrid}>
        {parse.columns_detected.map((column, index) => {
          const mapped = REQUIRED.includes(column) || OPTIONAL.includes(column);
          return (
            <label key={`${column}-${index}`} className={styles.mapRow}>
              <span className={mapped ? styles.mapped : styles.unmapped}>{column}</span>
              <select
                value={overrides[String(index)] ?? (mapped ? column : "")}
                onChange={(event) => onCorrect(String(index), event.target.value)}
              >
                {targets.map((t) => (
                  <option key={t || "none"} value={t}>
                    {t || "— ignore —"}
                  </option>
                ))}
              </select>
            </label>
          );
        })}
      </div>
    </div>
  );
}

function PreviewTable({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = Object.keys(rows[0] ?? {});
  return (
    <div className={styles.previewWrap}>
      <p className={styles.muted}>First {rows.length} rows, as they will be saved:</p>
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c}>{formatCell(row[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number") return String(value);
  const text = String(value);
  // Dates come back as ISO instants; the date is the part that matters here.
  const iso = /^(\d{4}-\d{2}-\d{2})T/.exec(text);
  return iso ? (iso[1] as string) : text;
}

/* ── file ────────────────────────────────────────────────────────── */

function FilePanel({ onImported }: { onImported: () => void }) {
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<IngestSummary | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function send(file: File) {
    setBusy(true);
    setError(null);
    try {
      setDone(await api.uploadFile(file));
      onImported();
    } catch (cause) {
      setError((cause as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  if (done) return <Imported summary={done} onAgain={() => setDone(null)} />;

  return (
    <section className={styles.panel}>
      <div
        className={`${styles.drop} ${dragging ? styles.dropActive : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files?.[0];
          if (file) void send(file);
        }}
        onClick={() => input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") input.current?.click();
        }}
      >
        <span className={styles.dropIcon} aria-hidden="true">
          ⬆
        </span>
        <strong>{busy ? "Uploading…" : "Drop a spreadsheet here"}</strong>
        <span className={styles.muted}>or click to choose · .xlsx, .csv, .parquet</span>
        <input
          ref={input}
          type="file"
          accept=".xlsx,.xls,.csv,.txt,.parquet"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void send(file);
            e.target.value = "";
          }}
        />
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/* ── shared ──────────────────────────────────────────────────────── */

function Imported({ summary, onAgain }: { summary: IngestSummary; onAgain: () => void }) {
  return (
    <section className={styles.panel}>
      <div className={styles.done} role="status">
        <h2>Imported</h2>
        <div className={styles.stats}>
          <Stat label="Added" value={summary.inserted.toLocaleString()} />
          <Stat
            label="Already present"
            value={summary.skipped_duplicates.toLocaleString()}
            muted={summary.skipped_duplicates === 0}
          />
          <Stat
            label="Rejected"
            value={summary.rejected_rows.toLocaleString()}
            muted={summary.rejected_rows === 0}
          />
          <Stat label="People" value={String(summary.users_seen)} />
        </div>
        {summary.skipped_duplicates > 0 && (
          <p className={styles.muted}>
            Duplicates are matched on the transaction itself, so re-importing the same file adds
            nothing.
          </p>
        )}
        {summary.rejections.length > 0 && (
          <ul className={styles.rejections}>
            {summary.rejections.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
        <button type="button" className={styles.secondary} onClick={onAgain}>
          Import more
        </button>
      </div>
    </section>
  );
}

function Stat({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className={`${styles.stat} ${muted ? styles.statMuted : ""}`}>
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}

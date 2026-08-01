import { useId, useState } from "react";
import DeltaBars from "./charts/DeltaBars";
import Donut from "./charts/Donut";
import IncomeBars from "./charts/IncomeBars";
import MerchantBars from "./charts/MerchantBars";
import TeamBars from "./charts/TeamBars";
import TrendLine from "./charts/TrendLine";
import UserBars from "./charts/UserBars";
import { formatMonth, money, titleCase } from "../lib/format";
import styles from "./ChartCard.module.css";
import { CHART_TITLES, type ChartTool, type DataSummary } from "../types";

interface Props {
  tool: ChartTool;
  summary: DataSummary;
  /** The server-rendered PNG. Kept as the export artifact, not the display. */
  pngUrl?: string | undefined;
}

type View = "chart" | "table";

export default function ChartCard({ tool, summary, pngUrl }: Props) {
  const [view, setView] = useState<View>("chart");
  const titleId = useId();

  const table = tableFor(tool, summary);
  const period = periodFor(tool, summary);

  return (
    <figure className={styles.card} aria-labelledby={titleId}>
      <div className={styles.head}>
        <figcaption id={titleId} className={styles.title}>
          {CHART_TITLES[tool]}
          {period && <span className={styles.badge}>{period}</span>}
        </figcaption>

        <div className={styles.actions}>
          <div className={styles.segmented} role="group" aria-label="Chart view">
            <button
              type="button"
              aria-pressed={view === "chart"}
              onClick={() => setView("chart")}
            >
              Chart
            </button>
            <button
              type="button"
              aria-pressed={view === "table"}
              onClick={() => setView("table")}
            >
              Table
            </button>
          </div>
          {pngUrl && (
            <a
              className={styles.download}
              href={pngUrl}
              target="_blank"
              rel="noopener"
              title="Open the rendered PNG"
              download
            >
              PNG
            </a>
          )}
        </div>
      </div>

      <div className={styles.body}>
        {view === "chart" ? (
          <ChartFor tool={tool} summary={summary} />
        ) : (
          <DataTable table={table} />
        )}
      </div>
    </figure>
  );
}

/** Does this summary carry the series its chart needs?
 *
 *  Checked in one place because two callers must agree: the parent decides
 *  whether to render a card at all, and this file decides what to draw in it.
 *  If they disagree, you get an empty card or a crash.
 *
 *  This cannot be replaced by trusting `no_data`. Transcripts persist in
 *  localStorage, so payloads written before that flag existed are still on
 *  disk and will be replayed on the next reload. */
export function hasSeries(tool: ChartTool, summary: DataSummary | undefined): boolean {
  const s = summary?.[tool] as Record<string, unknown> | undefined;
  if (!s || s["no_data"]) return false;
  const required: Record<ChartTool, string> = {
    plot_category_breakdown: "categories",
    plot_monthly_spending_trend: "monthly_totals",
    plot_income_vs_expense: "monthly",
    plot_top_merchants: "merchants",
    plot_period_comparison: "categories",
    plot_user_comparison: "categories",
    plot_team_overview: "people",
  };
  const series = s[required[tool]];
  return Array.isArray(series) && series.length > 0;
}


function ChartFor({ tool, summary }: { tool: ChartTool; summary: DataSummary }) {
  if (!hasSeries(tool, summary)) return null;
  if (tool === "plot_category_breakdown" && summary.plot_category_breakdown) {
    return <Donut summary={summary.plot_category_breakdown} />;
  }
  if (tool === "plot_monthly_spending_trend" && summary.plot_monthly_spending_trend) {
    return <TrendLine summary={summary.plot_monthly_spending_trend} />;
  }
  if (tool === "plot_income_vs_expense" && summary.plot_income_vs_expense) {
    return <IncomeBars summary={summary.plot_income_vs_expense} />;
  }
  if (tool === "plot_top_merchants" && summary.plot_top_merchants) {
    return <MerchantBars summary={summary.plot_top_merchants} />;
  }
  if (tool === "plot_period_comparison" && summary.plot_period_comparison) {
    return <DeltaBars summary={summary.plot_period_comparison} />;
  }
  if (tool === "plot_user_comparison" && summary.plot_user_comparison) {
    return <UserBars summary={summary.plot_user_comparison} />;
  }
  if (tool === "plot_team_overview" && summary.plot_team_overview) {
    return <TeamBars summary={summary.plot_team_overview} />;
  }
  return null;
}

/* ── the same numbers, as a table ──────────────────────────────────
   Not a fallback: a chart nobody can read is a chart nobody can check, and a
   screen-reader user gets the figures rather than a description of a picture. */

interface Table {
  columns: string[];
  rows: (string | number)[][];
}

function tableFor(tool: ChartTool, summary: DataSummary): Table | null {
  if (tool === "plot_category_breakdown") {
    const s = summary.plot_category_breakdown;
    if (!s?.categories) return null;
    return {
      columns: ["Category", "Amount", "Share"],
      rows: s.categories.map((c) => [titleCase(c.name), money(c.amount), `${c.share_pct}%`]),
    };
  }
  if (tool === "plot_monthly_spending_trend") {
    const s = summary.plot_monthly_spending_trend;
    if (!s?.monthly_totals) return null;
    return {
      columns: ["Month", "Spend"],
      rows: s.monthly_totals.map((m) => [formatMonth(m.month, "long"), money(m.expense)]),
    };
  }
  if (tool === "plot_team_overview") {
    const s = summary.plot_team_overview;
    if (!s?.people) return null;
    return {
      columns: ["Account holder", "Spend", "vs peer average"],
      rows: s.people.map((p) => [
        p.name,
        money(p.total),
        s.peer_average_excluding_focus != null && p.user_id === s.focus_user_id
          ? `${(s.focus_vs_peer_average ?? 0) > 0 ? "+" : ""}${money(s.focus_vs_peer_average ?? 0)}`
          : "—",
      ]),
    };
  }
  if (tool === "plot_user_comparison") {
    const s = summary.plot_user_comparison;
    if (!s?.categories) return null;
    return {
      columns: ["Category", s.left_user_name, s.right_user_name, "Who spent more"],
      rows: s.categories.map((c) => [
        titleCase(c.name),
        money(c.left),
        money(c.right),
        c.difference === 0
          ? "—"
          : `${(c.difference > 0 ? s.left_user_name : s.right_user_name).split(" ")[0]} +${money(Math.abs(c.difference))}`,
      ]),
    };
  }
  if (tool === "plot_period_comparison") {
    const s = summary.plot_period_comparison;
    if (!s?.categories) return null;
    return {
      columns: ["Category", s.compare_period_label ?? "Before", s.period_label ?? "After", "Change"],
      rows: s.categories.map((c) => [
        titleCase(c.name),
        money(c.previous),
        money(c.current),
        `${c.delta > 0 ? "+" : ""}${money(c.delta)}${c.delta_pct != null ? ` (${c.delta_pct > 0 ? "+" : ""}${c.delta_pct}%)` : ""}`,
      ]),
    };
  }
  if (tool === "plot_top_merchants") {
    const s = summary.plot_top_merchants;
    if (!s?.merchants) return null;
    return {
      columns: ["Merchant", "Amount", "Visits", "Share"],
      rows: s.merchants.map((m) => [m.name, money(m.amount), m.visits, `${m.share_pct}%`]),
    };
  }
  const s = summary.plot_income_vs_expense;
  if (!s?.monthly) return null;
  return {
    columns: ["Month", "Income", "Expense", "Net"],
    rows: s.monthly.map((m) => [formatMonth(m.month, "long"), money(m.income), money(m.expense), money(m.net)]),
  };
}

function DataTable({ table }: { table: Table | null }) {
  if (!table) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            {table.columns.map((column, index) => (
              <th key={column} scope="col" className={index === 0 ? "" : styles.numeric}>
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) =>
                cellIndex === 0 ? (
                  <th key={cellIndex} scope="row">
                    {cell}
                  </th>
                ) : (
                  <td key={cellIndex} className={styles.numeric}>
                    {cell}
                  </td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function periodFor(tool: ChartTool, summary: DataSummary): string | undefined {
  const s = summary[tool] as { period_label?: string; period?: string } | undefined;
  return s?.period_label ?? s?.period ?? summary.period_label;
}

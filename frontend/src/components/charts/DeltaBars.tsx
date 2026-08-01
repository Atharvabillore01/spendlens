import { compactMoney, money, titleCase } from "../../lib/format";
import styles from "./chart.module.css";
import type { PeriodComparisonSummary } from "../../types";

/* Diverging bars around a zero line.

   Direction is the entire message, so it is encoded in *position* — left of
   zero is a fall, right is a rise — not only in colour. The chart still reads
   in greyscale and to a colour-blind viewer, which a two-colour bar chart
   without a shared baseline would not. */

const W = 640;
const ROW = 30;
const PAD = { top: 8, right: 90, bottom: 8, left: 130 };

export default function DeltaBars({ summary }: { summary: PeriodComparisonSummary }) {
  const rows = summary.categories;
  if (rows.length === 0) return null;

  const extent = Math.max(...rows.map((r) => Math.abs(r.delta)), 1);
  const height = PAD.top + PAD.bottom + rows.length * ROW;
  const innerW = W - PAD.left - PAD.right;
  const zero = PAD.left + innerW / 2;
  const half = innerW / 2;
  const barH = Math.min(18, ROW * 0.6);

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`Change in spending, ${summary.period_label} versus ${
          summary.compare_period_label
        }. Overall ${summary.direction} ${money(Math.abs(summary.delta))}.`}
      >
        <line
          x1={zero}
          x2={zero}
          y1={PAD.top}
          y2={height - PAD.bottom}
          stroke="var(--ink-3)"
          strokeWidth={1}
        />
        {rows.map((row, i) => {
          const y = PAD.top + i * ROW;
          const width = (Math.abs(row.delta) / extent) * half;
          const rising = row.delta > 0;
          return (
            <g key={row.name}>
              <text
                className={styles.axisText}
                x={PAD.left - 12}
                y={y + ROW / 2 + 3}
                textAnchor="end"
              >
                {titleCase(row.name)}
              </text>
              <rect
                x={rising ? zero : zero - width}
                y={y + (ROW - barH) / 2}
                width={Math.max(width, 1)}
                height={barH}
                rx={3}
                fill={rising ? "var(--series-7)" : "var(--series-3)"}
              >
                <title>
                  {titleCase(row.name)}: {money(row.previous)} → {money(row.current)} (
                  {rising ? "+" : ""}
                  {money(row.delta)}
                  {row.delta_pct != null ? `, ${row.delta_pct > 0 ? "+" : ""}${row.delta_pct}%` : ", new"})
                </title>
              </rect>
              <text
                className={styles.valueLabel}
                x={rising ? zero + width + 8 : zero - width - 8}
                y={y + ROW / 2 + 4}
                textAnchor={rising ? "start" : "end"}
              >
                {rising ? "+" : ""}
                {compactMoney(row.delta)}
              </text>
            </g>
          );
        })}
      </svg>

      <ul className={styles.legend}>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-7)" }} />
          <span className={styles.legendName}>Spent more</span>
        </li>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-3)" }} />
          <span className={styles.legendName}>Spent less</span>
        </li>
      </ul>
      <p className={styles.footnote}>
        {summary.period_label} vs {summary.compare_period_label}
      </p>
    </>
  );
}

import { compactMoney, formatMonth, money } from "../../lib/format";
import styles from "./chart.module.css";
import type { MonthlyTrendSummary } from "../../types";

/* Line + rolling average on one scale. Selective direct labels: the peak and
   the endpoint get a value, the axis carries the rest — a number on every
   point goes unread. */

const W = 620;
const H = 260;
const PAD = { top: 26, right: 22, bottom: 30, left: 56 };

export default function TrendLine({ summary }: { summary: MonthlyTrendSummary }) {
  const points = summary.monthly_totals;
  if (points.length === 0) return null;

  const values = points.map((p) => p.expense);
  const max = Math.max(...values, 1);
  // Rolling average recomputed client-side from the same series the server
  // charted, so the two renderings cannot drift.
  const window = Math.max(1, Math.min(summary.rolling_window, values.length));
  const rolling = values.map((_, i) => {
    const slice = values.slice(Math.max(0, i - window + 1), i + 1);
    return slice.reduce((a, b) => a + b, 0) / slice.length;
  });

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const x = (i: number) =>
    PAD.left + (points.length === 1 ? innerW / 2 : (i / (points.length - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;

  const line = values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  const area = `${PAD.left},${PAD.top + innerH} ${line} ${x(values.length - 1)},${PAD.top + innerH}`;
  const avgLine = rolling.map((v, i) => `${x(i)},${y(v)}`).join(" ");

  const peak = values.indexOf(Math.max(...values));
  const last = values.length - 1;
  const ticks = [0, max / 2, max];

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Monthly spending over ${summary.months_covered} months. Average ${money(
          summary.average_monthly_spend,
        )}. Highest ${formatMonth(summary.highest_month.month, "long")} at ${money(summary.highest_month.expense)}.`}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line className={styles.grid} x1={PAD.left} x2={W - PAD.right} y1={y(tick)} y2={y(tick)} />
            <text className={styles.axisText} x={PAD.left - 8} y={y(tick) + 3} textAnchor="end">
              {compactMoney(tick)}
            </text>
          </g>
        ))}

        <polygon points={area} fill="var(--series-1)" opacity={0.1} />

        {values.length > 1 && (
          <polyline
            points={avgLine}
            fill="none"
            stroke="var(--ink-3)"
            strokeWidth={1.6}
            strokeDasharray="4 4"
          />
        )}

        <polyline
          points={line}
          fill="none"
          stroke="var(--series-1)"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {values.map((value, i) => (
          <circle key={i} className={styles.marker} cx={x(i)} cy={y(value)} r={4} fill="var(--series-1)">
            <title>
              {formatMonth(points[i]?.month ?? "", "long")} — {money(value)}
            </title>
          </circle>
        ))}

        <text
          className={styles.valueLabel}
          x={x(peak)}
          y={y(values[peak] ?? 0) - 12}
          textAnchor="middle"
        >
          {compactMoney(values[peak] ?? 0)}
        </text>
        {peak !== last && (
          <text
            className={styles.valueLabel}
            x={x(last)}
            y={y(values[last] ?? 0) - 12}
            textAnchor="end"
          >
            {compactMoney(values[last] ?? 0)}
          </text>
        )}

        {points.map((point, i) => (
          <text key={point.month} className={styles.axisText} x={x(i)} y={H - 8} textAnchor="middle">
            {formatMonth(point.month)}
          </text>
        ))}
      </svg>

      <ul className={styles.legend}>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-1)" }} />
          <span className={styles.legendName}>Monthly spend</span>
        </li>
        {values.length > 1 && (
          <li className={styles.legendItem}>
            <span className={styles.swatch} style={{ background: "var(--ink-3)" }} />
            <span className={styles.legendName}>{window}-month rolling average</span>
          </li>
        )}
      </ul>
    </>
  );
}

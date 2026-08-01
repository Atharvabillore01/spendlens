import { compactMoney, money, titleCase } from "../../lib/format";
import styles from "./chart.module.css";
import type { CategoryBreakdownSummary } from "../../types";

/* Donut drawn as one circle per slice using stroke-dasharray, rotated so the
   first slice starts at twelve o'clock. The gap between slices is surface
   colour laid over the ring, not a stroke on the mark itself.

   The legend carries value *and* share for every segment, so nothing here is
   encoded by colour alone. */

const R = 70;
const STROKE = 26;
const CIRCUMFERENCE = 2 * Math.PI * R;
const GAP = 2; // px of ring, converted to dash units below

/** "Other" is always the neutral slot; everything else takes a series hue in
 *  fixed order and never cycles past the ramp. */
function colorFor(name: string, index: number): string {
  if (name.toLowerCase() === "other") return "var(--series-8)";
  return `var(--series-${(index % 7) + 1})`;
}

export default function Donut({ summary }: { summary: CategoryBreakdownSummary }) {
  const slices = summary.categories;
  const total = summary.total_spend || 1;

  let cursor = 0;
  const arcs = slices.map((slice, index) => {
    const fraction = slice.amount / total;
    const length = Math.max(fraction * CIRCUMFERENCE - GAP, 0.5);
    const arc = {
      key: slice.name,
      color: colorFor(slice.name, index),
      dash: `${length} ${CIRCUMFERENCE - length}`,
      offset: -cursor,
      slice,
    };
    cursor += fraction * CIRCUMFERENCE;
    return arc;
  });

  return (
    <div className={styles.donutRow}>
      <div className={styles.donutFigure}>
        <svg
          className={styles.svg}
          viewBox="0 0 200 200"
          role="img"
          aria-label={`Spending by category. Total ${money(summary.total_spend)}. Largest: ${titleCase(
            summary.top_category.name,
          )} at ${money(summary.top_category.amount)}, ${summary.top_category.share_pct} percent.`}
        >
          <g transform="rotate(-90 100 100)">
            {arcs.map((arc) => (
              <circle
                key={arc.key}
                className={styles.slice}
                cx={100}
                cy={100}
                r={R}
                fill="none"
                stroke={arc.color}
                strokeWidth={STROKE}
                strokeDasharray={arc.dash}
                strokeDashoffset={arc.offset}
              >
                <title>
                  {titleCase(arc.slice.name)} — {money(arc.slice.amount)} ({arc.slice.share_pct}%)
                </title>
              </circle>
            ))}
          </g>
          <text className={styles.centerValue} x={100} y={98} textAnchor="middle">
            {compactMoney(summary.total_spend)}
          </text>
          <text className={styles.centerLabel} x={100} y={118} textAnchor="middle">
            total spend
          </text>
        </svg>
      </div>

      <div className={styles.donutLegend}>
        <ul className={styles.legend}>
          {arcs.map((arc) => (
            <li key={arc.key} className={styles.legendItem}>
              <span className={styles.swatch} style={{ background: arc.color }} />
              <span className={styles.legendName}>{titleCase(arc.slice.name)}</span>
              <span className={styles.legendValue}>{compactMoney(arc.slice.amount)}</span>
              <span className={styles.legendShare}>{arc.slice.share_pct}%</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

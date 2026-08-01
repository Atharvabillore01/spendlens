import { compactMoney, money, titleCase } from "../../lib/format";
import styles from "./chart.module.css";
import type { UserComparisonSummary } from "../../types";

/* Two people, one shared axis.
 *
 * Grouped rather than diverging: these are two independent quantities, not a
 * change in one. A diverging form would imply the second person is a baseline
 * the first moved away from, which is a relationship the data does not contain. */

const W = 640;
const ROW = 38;
const PAD = { top: 8, right: 96, bottom: 8, left: 130 };

export default function UserBars({ summary }: { summary: UserComparisonSummary }) {
  const rows = summary.categories;
  if (rows.length === 0) return null;

  const max = Math.max(...rows.flatMap((r) => [r.left, r.right]), 1);
  const height = PAD.top + PAD.bottom + rows.length * ROW;
  const innerW = W - PAD.left - PAD.right;
  const barH = 13;

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`${summary.left_user_name} spent ${money(summary.left_total)} against ${
          summary.right_user_name
        } at ${money(summary.right_total)} in ${summary.period_label ?? summary.period}.`}
      >
        {rows.map((row, i) => {
          const y = PAD.top + i * ROW;
          const leftW = Math.max((row.left / max) * innerW, 1);
          const rightW = Math.max((row.right / max) * innerW, 1);
          return (
            <g key={row.name}>
              <text className={styles.axisText} x={PAD.left - 10} y={y + ROW / 2 + 3} textAnchor="end">
                {titleCase(row.name)}
              </text>
              <rect x={PAD.left} y={y + 4} width={leftW} height={barH} rx={2} fill="var(--series-1)">
                <title>
                  {summary.left_user_name} — {money(row.left)}
                </title>
              </rect>
              <rect x={PAD.left} y={y + 6 + barH} width={rightW} height={barH} rx={2} fill="var(--series-4)">
                <title>
                  {summary.right_user_name} — {money(row.right)}
                </title>
              </rect>
              {/* Named, not signed: "-$4,728" next to two bars gives no clue
                  whose perspective the minus is from. */}
              <text className={styles.valueLabel} x={PAD.left + Math.max(leftW, rightW) + 8} y={y + ROW / 2 + 4}>
                {row.difference === 0
                  ? "—"
                  : `${(row.difference > 0 ? summary.left_user_name : summary.right_user_name).split(" ")[0]} +${compactMoney(Math.abs(row.difference))}`}
              </text>
            </g>
          );
        })}
      </svg>

      <ul className={styles.legend}>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-1)" }} />
          <span className={styles.legendName}>{summary.left_user_name}</span>
          <span className={styles.legendValue}>{compactMoney(summary.left_total)}</span>
        </li>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-4)" }} />
          <span className={styles.legendName}>{summary.right_user_name}</span>
          <span className={styles.legendValue}>{compactMoney(summary.right_total)}</span>
        </li>
      </ul>
      {summary.higher_spender && (
        <p className={styles.footnote}>
          {summary.higher_spender} spent {compactMoney(summary.gap)} more than{" "}
          {summary.lower_spender}
          {summary.higher_spent_pct_more_than_lower != null &&
            ` — ${summary.higher_spent_pct_more_than_lower}% more`}
        </p>
      )}
    </>
  );
}

import { compactMoney, formatMonth, money } from "../../lib/format";
import styles from "./chart.module.css";
import type { IncomeVsExpenseSummary } from "../../types";

/* Income, expense and net are all dollars, so they share **one** y-axis. A
   twin axis would make the net line's position relative to the bars arbitrary,
   inventing a relationship the data doesn't contain. */

const W = 640;
const H = 280;
const PAD = { top: 26, right: 26, bottom: 32, left: 58 };

export default function IncomeBars({ summary }: { summary: IncomeVsExpenseSummary }) {
  const months = summary.monthly;
  if (months.length === 0) return null;

  const highest = Math.max(...months.map((m) => Math.max(m.income, m.expense, m.net)), 1);
  const lowest = Math.min(...months.map((m) => Math.min(m.net, 0)), 0);

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const span = highest - lowest || 1;
  const y = (v: number) => PAD.top + innerH - ((v - lowest) / span) * innerH;

  const slot = innerW / months.length;
  const barW = Math.min(22, slot * 0.3);
  const center = (i: number) => PAD.left + slot * i + slot / 2;

  const netLine = months.map((m, i) => `${center(i)},${y(m.net)}`).join(" ");
  const ticks = [lowest, (lowest + highest) / 2, highest];

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Income versus expenses across ${summary.months_covered} months. Net ${money(
          summary.net_savings,
        )}. ${summary.months_in_deficit} of ${summary.months_covered} months in deficit.`}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line className={styles.grid} x1={PAD.left} x2={W - PAD.right} y1={y(tick)} y2={y(tick)} />
            <text className={styles.axisText} x={PAD.left - 8} y={y(tick) + 3} textAnchor="end">
              {compactMoney(tick)}
            </text>
          </g>
        ))}

        {/* Zero baseline sits above the grid but below the marks. */}
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={y(0)}
          y2={y(0)}
          stroke="var(--ink-3)"
          strokeWidth={1}
        />

        {months.map((month, i) => {
          const incomeTop = y(Math.max(month.income, 0));
          const expenseTop = y(Math.max(month.expense, 0));
          return (
            <g key={month.month}>
              <rect
                x={center(i) - barW - 1}
                y={incomeTop}
                width={barW}
                height={Math.max(y(0) - incomeTop, 0)}
                fill="var(--series-3)"
                rx={2}
              >
                <title>
                  {formatMonth(month.month, "long")} income — {money(month.income)}
                </title>
              </rect>
              <rect
                x={center(i) + 1}
                y={expenseTop}
                width={barW}
                height={Math.max(y(0) - expenseTop, 0)}
                fill="var(--series-7)"
                rx={2}
              >
                <title>
                  {formatMonth(month.month, "long")} expense — {money(month.expense)}
                </title>
              </rect>
            </g>
          );
        })}

        {summary.show_net_line && (
          <>
            <polyline
              points={netLine}
              fill="none"
              stroke="var(--series-1)"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            {months.map((month, i) => (
              <circle
                key={month.month}
                className={styles.marker}
                cx={center(i)}
                cy={y(month.net)}
                r={4}
                fill="var(--series-1)"
              >
                <title>
                  {formatMonth(month.month, "long")} net — {money(month.net)}
                </title>
              </circle>
            ))}
          </>
        )}

        {months.map((month, i) => (
          <text key={month.month} className={styles.axisText} x={center(i)} y={H - 10} textAnchor="middle">
            {formatMonth(month.month)}
          </text>
        ))}
      </svg>

      <ul className={styles.legend}>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-3)" }} />
          <span className={styles.legendName}>Income</span>
        </li>
        <li className={styles.legendItem}>
          <span className={styles.swatch} style={{ background: "var(--series-7)" }} />
          <span className={styles.legendName}>Expense</span>
        </li>
        {summary.show_net_line && (
          <li className={styles.legendItem}>
            <span className={styles.swatch} style={{ background: "var(--series-1)" }} />
            <span className={styles.legendName}>Net savings</span>
          </li>
        )}
      </ul>
    </>
  );
}

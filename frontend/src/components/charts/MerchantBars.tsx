import { compactMoney, money } from "../../lib/format";
import styles from "./chart.module.css";
import type { TopMerchantsSummary } from "../../types";

/* Horizontal bars, ranked, tallest first.

   Horizontal because merchant names are text of variable length: rotated under
   a vertical axis they become unreadable, and truncating them loses the one
   thing the chart is about. Every bar carries its value directly -- the ranking
   is the message, so the axis is nearly redundant and stays recessive. */

const W = 640;
const ROW = 30;
const PAD = { top: 8, right: 96, bottom: 8, left: 150 };

export default function MerchantBars({ summary }: { summary: TopMerchantsSummary }) {
  const merchants = summary.merchants;
  if (merchants.length === 0) return null;

  const max = Math.max(...merchants.map((m) => m.amount), 1);
  const height = PAD.top + PAD.bottom + merchants.length * ROW;
  const innerW = W - PAD.left - PAD.right;
  const barH = Math.min(18, ROW * 0.62);

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`Top ${merchants.length} merchants of ${summary.merchant_count}. ${
          summary.top_merchant.name
        } is highest at ${money(summary.top_merchant.amount)} over ${
          summary.top_merchant.visits
        } transactions.`}
      >
        {merchants.map((merchant, i) => {
          const y = PAD.top + i * ROW;
          const width = Math.max((merchant.amount / max) * innerW, 2);
          return (
            <g key={merchant.name}>
              <text
                className={styles.axisText}
                x={PAD.left - 10}
                y={y + ROW / 2 + 3}
                textAnchor="end"
              >
                {merchant.name.length > 22 ? merchant.name.slice(0, 21) + "…" : merchant.name}
              </text>
              <rect
                x={PAD.left}
                y={y + (ROW - barH) / 2}
                width={width}
                height={barH}
                rx={3}
                fill="var(--series-1)"
              >
                <title>
                  {merchant.name} — {money(merchant.amount)} across {merchant.visits}{" "}
                  {merchant.visits === 1 ? "transaction" : "transactions"} ({merchant.share_pct}%)
                </title>
              </rect>
              <text
                className={styles.valueLabel}
                x={PAD.left + width + 8}
                y={y + ROW / 2 + 4}
                textAnchor="start"
              >
                {compactMoney(merchant.amount)}
              </text>
            </g>
          );
        })}
      </svg>

      <p className={styles.footnote}>
        {merchants.length} of {summary.merchant_count} merchants ·{" "}
        {compactMoney(summary.total_spend)} total
      </p>
    </>
  );
}

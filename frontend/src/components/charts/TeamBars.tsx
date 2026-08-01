import { compactMoney, money } from "../../lib/format";
import styles from "./chart.module.css";
import type { TeamOverviewSummary } from "../../types";

/* Everyone, ranked, with the peer average as a reference line.
 *
 * The line is the average of the *others* — an average someone is inside of is
 * pulled toward them, which understates the gap and does so worst when there
 * are fewest accounts. The focus account is the only coloured bar so the
 * comparison it invites is unambiguous. */

const W = 640;
const ROW = 34;
const PAD = { top: 10, right: 96, bottom: 26, left: 140 };

export default function TeamBars({ summary }: { summary: TeamOverviewSummary }) {
  const people = summary.people;
  if (people.length === 0) return null;

  const average = summary.peer_average_excluding_focus;
  const max = Math.max(...people.map((p) => p.total), average ?? 0, 1);
  const height = PAD.top + PAD.bottom + people.length * ROW;
  const innerW = W - PAD.left - PAD.right;
  const barH = 16;
  const x = (v: number) => PAD.left + (v / max) * innerW;

  return (
    <>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`${summary.account_holders} account holders. ${
          summary.focus_user_name ?? "The selected account"
        } spent ${money(summary.focus_total ?? 0)}${
          average != null ? ` against a peer average of ${money(average)}` : ""
        }.`}
      >
        {people.map((person, i) => {
          const y = PAD.top + i * ROW;
          const isFocus = person.user_id === summary.focus_user_id;
          return (
            <g key={person.user_id}>
              <text
                className={styles.axisText}
                x={PAD.left - 10}
                y={y + ROW / 2 + 3}
                textAnchor="end"
                style={isFocus ? { fontWeight: 650, fill: "var(--ink)" } : undefined}
              >
                {person.name}
              </text>
              <rect
                x={PAD.left}
                y={y + (ROW - barH) / 2}
                width={Math.max(x(person.total) - PAD.left, 1)}
                height={barH}
                rx={3}
                fill={isFocus ? "var(--series-1)" : "var(--series-8)"}
              >
                <title>
                  {person.name} — {money(person.total)}
                </title>
              </rect>
              <text className={styles.valueLabel} x={x(person.total) + 8} y={y + ROW / 2 + 4}>
                {compactMoney(person.total)}
              </text>
            </g>
          );
        })}

        {average != null && (
          <g>
            <line
              x1={x(average)}
              x2={x(average)}
              y1={PAD.top - 2}
              y2={height - PAD.bottom + 2}
              stroke="var(--series-7)"
              strokeWidth={1.5}
              strokeDasharray="5 4"
            />
            <text className={styles.axisText} x={x(average)} y={height - PAD.bottom + 16} textAnchor="middle">
              peer avg {compactMoney(average)}
            </text>
          </g>
        )}
      </svg>

      {summary.focus_vs_peer_average != null && summary.focus_user_name && (
        <p className={styles.footnote}>
          {summary.focus_user_name} is {compactMoney(Math.abs(summary.focus_vs_peer_average))}{" "}
          {summary.focus_is_above_average ? "above" : "below"} the average of the other{" "}
          {summary.account_holders - 1} account{summary.account_holders === 2 ? "" : "s"}
        </p>
      )}
    </>
  );
}

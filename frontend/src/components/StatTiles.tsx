import { useEffect, useRef } from "react";
import { compactMoney, formatMonth, prefersReducedMotion, signedPct, titleCase } from "../lib/format";
import styles from "./StatTiles.module.css";
import type { DataSummary } from "../types";

interface Tile {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg";
}

/* "The answer is a number" is its own form. These come straight from
   data_summary — i.e. from Pandas — never from the model's prose.

   Each branch checks the fields it will actually read, not just the `no_data`
   flag. An empty window returns a summary with the tool's key present but its
   figures absent, and a UI that trusts the flag alone crashes on the first
   query that finds nothing. Defensive here is cheaper than an error boundary
   catching it after the render has already failed. */
function tilesFor(summary: DataSummary): Tile[] {
  const breakdown = summary.plot_category_breakdown;
  if (breakdown && !breakdown.no_data && breakdown.top_category && breakdown.categories) {
    return [
      { label: "Total spend", value: compactMoney(breakdown.total_spend) },
      {
        label: "Top category",
        value: titleCase(breakdown.top_category.name),
        sub: `${compactMoney(breakdown.top_category.amount)} · ${breakdown.top_category.share_pct}%`,
      },
      { label: "Categories", value: String(breakdown.categories.length) },
    ];
  }

  const trend = summary.plot_monthly_spending_trend;
  if (trend && !trend.no_data && trend.highest_month) {
    const tiles: Tile[] = [
      { label: "Average / month", value: compactMoney(trend.average_monthly_spend) },
      {
        label: "Highest month",
        value: compactMoney(trend.highest_month.expense),
        sub: formatMonth(trend.highest_month.month, "long"),
      },
    ];
    if (trend.change_pct_first_to_last != null) {
      tiles.push({
        label: "Change",
        value: signedPct(trend.change_pct_first_to_last),
        tone: trend.change_pct_first_to_last > 0 ? "neg" : "pos",
        sub: "first → last month",
      });
    }
    return tiles;
  }

  const flow = summary.plot_income_vs_expense;
  if (flow && !flow.no_data && flow.net_savings != null) {
    const tiles: Tile[] = [
      {
        label: "Net savings",
        value: compactMoney(flow.net_savings),
        tone: flow.net_savings >= 0 ? "pos" : "neg",
      },
    ];
    if (flow.savings_rate_pct != null) {
      tiles.push({ label: "Savings rate", value: `${flow.savings_rate_pct}%`, sub: "of income" });
    }
    tiles.push({
      label: "Months in deficit",
      value: `${flow.months_in_deficit} of ${flow.months_covered}`,
      tone: flow.months_in_deficit ? "neg" : "pos",
    });
    return tiles;
  }

  const versus = summary.plot_user_comparison;
  if (versus && !versus.no_data && versus.left_total != null) {
    return [
      { label: versus.left_user_name, value: compactMoney(versus.left_total) },
      { label: versus.right_user_name, value: compactMoney(versus.right_total) },
      {
        label: "Difference",
        value: compactMoney(Math.abs(versus.difference)),
        sub:
          versus.higher_spender && versus.higher_spent_pct_more_than_lower != null
            ? `${versus.higher_spender.split(" ")[0]} +${versus.higher_spent_pct_more_than_lower}%`
            : versus.higher_spender
              ? `${versus.higher_spender} spent more`
              : undefined,
        tone: versus.difference > 0 ? "neg" : "pos",
      },
    ];
  }

  const change = summary.plot_period_comparison;
  if (change && !change.no_data && change.current_total != null) {
    const tiles: Tile[] = [
      {
        label: change.direction === "down" ? "Spent less" : "Spent more",
        value: compactMoney(Math.abs(change.delta)),
        // For spending, up is the bad direction — the tone follows meaning,
        // not the sign of the number.
        tone: change.direction === "up" ? "neg" : "pos",
        sub: change.delta_pct != null ? `${signedPct(change.delta_pct)} vs before` : "vs before",
      },
      {
        label: change.period_label ?? "This period",
        value: compactMoney(change.current_total),
        sub: `was ${compactMoney(change.previous_total)}`,
      },
    ];
    if (change.biggest_increase) {
      tiles.push({
        label: "Biggest rise",
        value: titleCase(change.biggest_increase.name),
        sub: `+${compactMoney(change.biggest_increase.delta)}`,
      });
    }
    return tiles;
  }

  const merchants = summary.plot_top_merchants;
  if (merchants && !merchants.no_data && merchants.top_merchant) {
    return [
      { label: "Top merchant", value: merchants.top_merchant.name },
      {
        label: "Spent there",
        value: compactMoney(merchants.top_merchant.amount),
        sub: `${merchants.top_merchant.visits} transactions · ${merchants.top_merchant.share_pct}%`,
      },
      { label: "Merchants", value: String(merchants.merchant_count) },
    ];
  }

  // No chart this turn: fall back to the window summary the pipeline computed.
  if (summary.top_category) {
    const tiles: Tile[] = [
      { label: "Total spend", value: compactMoney(summary.total_spend ?? 0) },
      {
        label: "Top category",
        value: titleCase(summary.top_category.name),
        sub: compactMoney(summary.top_category.amount),
      },
    ];
    if (summary.net_savings != null) {
      tiles.push({
        label: "Net",
        value: compactMoney(summary.net_savings),
        tone: summary.net_savings >= 0 ? "pos" : "neg",
      });
    }
    return tiles;
  }

  return [];
}

export default function StatTiles({ summary }: { summary: DataSummary }) {
  const tiles = tilesFor(summary);
  if (!tiles.length) return null;

  return (
    <div className={styles.stats}>
      {tiles.map((tile) => (
        <div key={tile.label} className={styles.stat}>
          <span className={styles.label}>{tile.label}</span>
          <CountUp
            className={`${styles.value} ${tile.tone ? styles[tile.tone] : ""}`}
            text={tile.value}
          />
          {tile.sub && <span className={styles.sub}>{tile.sub}</span>}
        </div>
      ))}
    </div>
  );
}

/** Counts the numeric part of a KPI up to its final value, preserving the
 *  surrounding format ("$", "%", "4 of 6"). Skipped under reduced-motion, and
 *  the final text is committed synchronously either way so the DOM is never
 *  left holding a partial number. */
function CountUp({ text, className }: { text: string; className: string }) {
  const node = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const element = node.current;
    if (!element) return;
    element.textContent = text;

    if (prefersReducedMotion()) return;
    const match = text.match(/-?[\d,]+(?:\.\d+)?/);
    if (!match) return;
    const target = Number(match[0].replace(/,/g, ""));
    if (!Number.isFinite(target) || Math.abs(target) < 10) return;

    const decimals = (match[0].split(".")[1] ?? "").length;
    const started = performance.now();
    const DURATION = 620;
    let raf = 0;

    const frame = (now: number) => {
      const t = Math.min((now - started) / DURATION, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      const current = (target * eased).toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
      element.textContent = text.replace(match[0], current);
      if (t < 1) raf = requestAnimationFrame(frame);
      else element.textContent = text;
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      element.textContent = text;
    };
  }, [text]);

  return <span ref={node} className={className} />;
}

import { formatMonth, titleCase } from "./format";
import type { QueryResult } from "../types";

/** Follow-ups are derived from what the pipeline actually computed, so every
 *  suggestion is answerable from this user's data rather than a generic list.
 *
 *  In the console the same derivation holds, but the phrasing cannot: "Am I
 *  saving money?" has no subject on a screen whose thread covers every account,
 *  and clicking it asks a question about whichever account was the anchor. The
 *  subject the answer was actually about is carried through instead, so a
 *  follow-up stays on the client the manager was just reading. */
export function followUps(result: QueryResult, options: { console?: boolean } = {}): string[] {
  if (options.console) return consoleFollowUps(result);
  return personalFollowUps(result);
}

function consoleFollowUps(result: QueryResult): string[] {
  const summary = result.data_summary ?? {};
  const breakdown = summary.plot_category_breakdown;
  const trend = summary.plot_monthly_spending_trend;
  // Whoever this answer was about. Without a handle there is nobody to follow
  // up *on*, so the suggestions stay team-shaped.
  const subject = (result.user_name ?? "").trim().split(/\s+/)[0];
  const at = subject ? `@${subject} ` : "";
  const out: string[] = [];

  if (subject && breakdown?.top_category && !breakdown.parent_category) {
    out.push(`${at}break down their ${titleCase(breakdown.top_category.name).toLowerCase()} spending`);
  }
  if (subject && !trend) out.push(`${at}how has their spending changed over time?`);
  if (subject && !summary.plot_top_merchants) out.push(`${at}show their top merchants`);
  out.push("Compare spending across the team");
  out.push("Who spent the most last month?");

  return out.slice(0, 3);
}

function personalFollowUps(result: QueryResult): string[] {
  const summary = result.data_summary ?? {};
  const breakdown = summary.plot_category_breakdown;
  const trend = summary.plot_monthly_spending_trend;
  const income = summary.plot_income_vs_expense;
  const out: string[] = [];

  if (breakdown?.top_category && !breakdown.parent_category) {
    out.push(`Break down my ${titleCase(breakdown.top_category.name).toLowerCase()} spending`);
  }
  if (breakdown && !trend) out.push("How has that changed over time?");
  if (trend?.highest_month) out.push(`What drove ${formatMonth(trend.highest_month.month, "long")}?`);
  if (income && income.months_in_deficit > 0) out.push("Which months did I overspend?");
  if (income && !breakdown) out.push("Where is my money going?");
  if (!income) out.push("Am I saving money?");
  if (!summary.plot_top_merchants) out.push("Show me my top merchants");

  return out.slice(0, 3);
}

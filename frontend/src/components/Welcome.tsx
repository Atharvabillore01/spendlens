import { Suspense, lazy } from "react";
import styles from "./Welcome.module.css";
import type { User } from "../types";

/* three.js is ~700kB of the bundle and this hero is decoration — it must not
   sit in the critical path. Split out, it arrives after the page is usable and
   never blocks a user who came here to ask a question. */
const HeroField = lazy(() => import("./three/HeroField"));

interface Props {
  /** Console mode reframes the empty state around the team, and offers
   *  team-shaped openers instead of first-person ones. */
  console?: boolean;
  /** The roster, for team totals and the client-by-client row. */
  users?: User[];
  /** Starts a mention for one client rather than asking immediately: naming
   *  someone is the beginning of a question, not the whole of it. */
  onMention?: (userName: string) => void;
  user: User | null;
  onAsk: (prompt: string) => void;
}

interface Starter {
  icon: string;
  title: string;
  hint: string;
}

/* Starters describe what the user gets, not which tool fires. Each one maps to
   a different capability, so the first click is never a dead end. */
const STARTERS: Starter[] = [
  {
    icon: "◒",
    title: "What did I spend the most on last month?",
    hint: "Your biggest category, with the breakdown",
  },
  {
    icon: "◈",
    title: "Am I saving money?",
    hint: "Income against spending, month by month",
  },
  {
    icon: "◇",
    title: "Show me my spending trend",
    hint: "How your monthly total has moved",
  },
  {
    icon: "◍",
    title: "Give me a full financial report",
    hint: "Everything worth knowing, in one answer",
  },
];

/* A console asks different questions. "Am I saving money?" is meaningless when
   the thread covers every account, and offering it invites an answer about
   whichever account happened to be the anchor. These are questions the
   cross-account tools can actually answer. */
const TEAM_STARTERS: Starter[] = [
  {
    icon: "◍",
    title: "Compare spending across the team",
    hint: "Every account, side by side",
  },
  {
    icon: "◑",
    title: "Who spent the most last month?",
    hint: "Ranked, with the gap between them",
  },
  {
    icon: "◈",
    title: "How does the team's spending split by category?",
    hint: "Where the money goes across all accounts",
  },
  {
    icon: "◇",
    title: "Has team spending gone up or down?",
    hint: "The trend across the whole book",
  },
];

export default function Welcome({ user, users = [], onAsk, console: isConsole, onMention }: Props) {
  const firstName = user?.user_name.split(" ")[0];
  const starters = isConsole ? TEAM_STARTERS : STARTERS;
  const teamTransactions = users.reduce((sum, u) => sum + (u.transaction_count || 0), 0);

  return (
    <div className={styles.welcome}>
      <Suspense fallback={<div className={styles.heroSlot} />}>
        <HeroField />
      </Suspense>

      <h2>
        {isConsole
          ? "Ask about the team"
          : firstName
            ? `Ask ${firstName}'s money anything`
            : "Select an account"}
      </h2>
      <p>
        {isConsole
          ? "A question with no name covers every account. Type @ and a first name to ask about one of them."
          : "Every figure comes straight from your transactions and is checked before it reaches you — the assistant explains the numbers, it never invents them."}
      </p>

      {(user || isConsole) && (
        <>
          <div className={styles.grid}>
            {starters.map((starter, index) => (
              <button
                key={starter.title}
                type="button"
                className={styles.card}
                style={{ ["--i" as string]: String(index) }}
                onClick={() => onAsk(starter.title)}
              >
                <span className={styles.icon} aria-hidden="true">
                  {starter.icon}
                </span>
                <span className={styles.body}>
                  <b>{starter.title}</b>
                  <small>{starter.hint}</small>
                </span>
              </button>
            ))}
          </div>

          {/* Client by client. The console's other half: one click names an
              account, so a question about one person is never buried in a
              team-wide answer. */}
          {isConsole && users.length > 0 && (
            <div className={styles.clients}>
              <p className={styles.clientsLabel}>Or ask about one client</p>
              <div className={styles.clientRow}>
                {users.map((client) => (
                  <button
                    key={client.user_id}
                    type="button"
                    className={styles.client}
                    onClick={() => onMention?.(client.user_name)}
                  >
                    <b>{client.user_name}</b>
                    <small>{client.transaction_count.toLocaleString()} transactions</small>
                  </button>
                ))}
              </div>
            </div>
          )}

          <p className={styles.foot}>
            {isConsole
              ? `${users.length} account${users.length === 1 ? "" : "s"} · ${teamTransactions.toLocaleString()} transactions on file`
              : `${(user?.transaction_count ?? 0).toLocaleString()} transactions on file`}
          </p>
        </>
      )}
    </div>
  );
}

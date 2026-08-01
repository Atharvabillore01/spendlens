import { Suspense, lazy } from "react";
import styles from "./Welcome.module.css";
import type { User } from "../types";

/* three.js is ~700kB of the bundle and this hero is decoration — it must not
   sit in the critical path. Split out, it arrives after the page is usable and
   never blocks a user who came here to ask a question. */
const HeroField = lazy(() => import("./three/HeroField"));

interface Props {
  user: User | null;
  onAsk: (prompt: string) => void;
}

/* Starters describe what the user gets, not which tool fires. Each one maps to
   a different capability, so the first click is never a dead end. */
const STARTERS = [
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

export default function Welcome({ user, onAsk }: Props) {
  const firstName = user?.user_name.split(" ")[0];

  return (
    <div className={styles.welcome}>
      <Suspense fallback={<div className={styles.heroSlot} />}>
        <HeroField />
      </Suspense>

      <h2>{firstName ? `Ask ${firstName}'s money anything` : "Select an account"}</h2>
      <p>
        Every figure comes straight from your transactions and is checked before it reaches
        you — the assistant explains the numbers, it never invents them.
      </p>

      {user && (
        <>
          <div className={styles.grid}>
            {STARTERS.map((starter, index) => (
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

          <p className={styles.foot}>
            {user.transaction_count.toLocaleString()} transactions on file
          </p>
        </>
      )}
    </div>
  );
}

import { initials } from "../lib/format";
import styles from "./ChatHeader.module.css";
import type { User } from "../types";

interface Props {
  user: User | null;
  asOf: string;
  turnCount: number;
  onClear: () => void;
  /** Console mode: the thread spans every account, so naming one at the top
   *  would be a lie about what the next answer covers. */
  console?: boolean;
  accountCount?: number;
}

export default function ChatHeader({
  user,
  asOf,
  turnCount,
  onClear,
  console: isConsole,
  accountCount = 0,
}: Props) {
  return (
    <header className={styles.head}>
      <div className={styles.who}>
        <span className={`${styles.avatar} ${user || isConsole ? styles.brandish : ""}`}>
          {isConsole ? "◍" : user ? initials(user.user_name) : "–"}
        </span>
        <div>
          <h1>{isConsole ? "Team console" : (user?.user_name ?? "Select an account")}</h1>
          <p>
            {isConsole
              ? `${accountCount} account${accountCount === 1 ? "" : "s"} · through ${asOf}`
              : user
                ? `${user.transaction_count.toLocaleString()} transactions · through ${asOf}`
                : "no account selected"}
          </p>
        </div>
      </div>

      {turnCount > 0 && (
        <button type="button" className={styles.ghostBtn} onClick={onClear} title="Clear this conversation">
          Clear
        </button>
      )}
    </header>
  );
}

import { initials } from "../lib/format";
import styles from "./ChatHeader.module.css";
import type { User } from "../types";

interface Props {
  user: User | null;
  asOf: string;
  turnCount: number;
  onClear: () => void;
}

export default function ChatHeader({ user, asOf, turnCount, onClear }: Props) {
  return (
    <header className={styles.head}>
      <div className={styles.who}>
        <span className={`${styles.avatar} ${user ? styles.brandish : ""}`}>
          {user ? initials(user.user_name) : "–"}
        </span>
        <div>
          <h1>{user?.user_name ?? "Select an account"}</h1>
          <p>
            {user
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

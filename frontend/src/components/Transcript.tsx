import { useEffect, useRef } from "react";
import AssistantMessage from "./AssistantMessage";
import PendingMessage from "./PendingMessage";
import Welcome from "./Welcome";
import styles from "./Transcript.module.css";
import type { Turn, User } from "../types";

interface Props {
  turns: Turn[];
  user: User | null;
  busy: boolean;
  onAsk: (prompt: string) => void;
}

export default function Transcript({ turns, user, busy, onAsk }: Props) {
  const scroller = useRef<HTMLDivElement>(null);

  // Follow the tail as turns land. `scrollTo` respects the CSS smooth
  // behaviour, which the reduced-motion rule downgrades to instant.
  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns]);

  return (
    <div
      ref={scroller}
      className={styles.transcript}
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      aria-busy={busy}
    >
      {turns.length === 0 ? (
        <Welcome user={user} onAsk={onAsk} />
      ) : (
        turns.map((turn, index) => {
          const isLast = index === turns.length - 1;
          switch (turn.role) {
            case "user":
              return (
                <div key={turn.id} className={`${styles.msg} ${styles.user}`}>
                  <div className={styles.userBubble}>{turn.text}</div>
                </div>
              );
            case "pending":
              return <PendingMessage key={turn.id} />;
            case "error":
              return (
                <div key={turn.id} className={`${styles.msg} ${styles.assistant} ${styles.blocked}`}>
                  <span className={styles.avatar}>!</span>
                  <div className={styles.bubbleWrap}>
                    <div className={styles.bubble}>
                      <p>{turn.text}</p>
                    </div>
                  </div>
                </div>
              );
            case "assistant":
              return (
                <AssistantMessage
                  key={turn.id}
                  result={turn.result}
                  isLast={isLast}
                  onAsk={onAsk}
                />
              );
            default:
              return null;
          }
        })
      )}
    </div>
  );
}

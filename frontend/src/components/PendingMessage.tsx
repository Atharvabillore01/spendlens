import shared from "./Transcript.module.css";
import styles from "./PendingMessage.module.css";

/** The skeleton mirrors the shape of a real answer — prose, KPI row, chart —
 *  so the layout doesn't jump when the content lands. */
export default function PendingMessage() {
  return (
    <div className={`${shared.msg} ${shared.assistant}`}>
      <span className={shared.avatar}>◗</span>
      <div className={shared.bubbleWrap}>
        <div className={shared.bubble}>
          <div className={`${styles.skel} ${styles.line} ${styles.w90}`} />
          <div className={`${styles.skel} ${styles.line} ${styles.w70}`} />
          <div className={styles.row}>
            <div className={`${styles.skel} ${styles.tile}`} />
            <div className={`${styles.skel} ${styles.tile}`} />
            <div className={`${styles.skel} ${styles.tile}`} />
          </div>
          <div className={`${styles.skel} ${styles.block}`} />
          <p className={styles.note}>
            <span className={styles.spinner} />
            <span>Reading your transactions…</span>
          </p>
        </div>
      </div>
    </div>
  );
}

import { Component, type ErrorInfo, type ReactNode } from "react";
import styles from "./ErrorBoundary.module.css";

interface Props {
  children: ReactNode;
  /** Shown instead of the crashed subtree. */
  label?: string;
}

interface State {
  error: Error | null;
}

/* Contains a render failure to the message that caused it.
 *
 * Without this, one malformed `data_summary` unmounts the entire app — the
 * conversation, the sidebar, everything — and the user is left with a white
 * page and no way back. Scoped per message, a bad payload costs one card. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Left in deliberately: this is the breadcrumb that turns "it went blank"
    // into a stack trace someone can act on.
    console.error("Render failed in", this.props.label ?? "a component", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className={styles.fallback} role="alert">
          <strong>This part of the answer couldn&rsquo;t be displayed.</strong>
          <span>
            The figures behind it are still in the raw response. {this.props.label ?? ""}
          </span>
          <button type="button" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

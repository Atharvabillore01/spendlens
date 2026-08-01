import ChartCard, { hasSeries } from "./ChartCard";
import ErrorBoundary from "./ErrorBoundary";
import MetaStrip from "./MetaStrip";
import StatTiles from "./StatTiles";
import { followUps } from "../lib/followUps";
import { FLAGS } from "../lib/flags";
import shared from "./Transcript.module.css";
import styles from "./AssistantMessage.module.css";
import { CHART_TITLES, CHART_TOOLS, type ChartTool, type QueryResult } from "../types";

interface Props {
  result: QueryResult;
  isLast: boolean;
  onAsk: (prompt: string) => void;
}

export default function AssistantMessage({ result, isLast, onAsk }: Props) {
  const flags = result.guardrail_flags ?? [];
  const blocked = flags.some((flag) => FLAGS[flag]?.danger);
  const variant = blocked ? shared.blocked : result.degraded ? styles.degraded : "";

  // Charts are rendered from the series in `data_summary`, not from the PNG —
  // same numbers, but interactive, themeable and readable as a table.
  // One predicate, shared with the card: a summary that lacks its series
  // renders nothing rather than an empty frame or a crash. Old transcripts
  // restored from localStorage predate the `no_data` flag, so the check is on
  // the data itself.
  const charted = CHART_TOOLS.filter((tool) => hasSeries(tool, result.data_summary));

  return (
    <div className={`${shared.msg} ${shared.assistant} ${variant}`}>
      <span className={shared.avatar}>{blocked ? "!" : "◗"}</span>
      <div className={shared.bubbleWrap}>
        <div className={shared.bubble}>
          <Prose text={result.response} />

          <ErrorBoundary label="Stat tiles">
            <StatTiles summary={result.data_summary ?? {}} />
          </ErrorBoundary>

          {charted.length > 0 && (
            <div className={styles.charts}>
              {charted.map((tool) => (
                <ErrorBoundary key={tool} label={CHART_TITLES[tool as ChartTool]}>
                  <ChartCard
                    tool={tool as ChartTool}
                    summary={result.data_summary}
                    pngUrl={pngFor(result.visualizations, tool)}
                  />
                </ErrorBoundary>
              ))}
            </div>
          )}

          <MetaStrip result={result} />
        </div>

        {isLast && <FollowUps result={result} onAsk={onAsk} />}
      </div>
    </div>
  );
}

/** The model emits light markdown (**bold**). Rendered as text nodes so
 *  nothing in the response can inject markup. */
function Prose({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n{2,}/).map((paragraph, index) => (
        <p key={index}>
          {paragraph.split(/(\*\*[^*]+\*\*)/g).map((part, partIndex) =>
            part.startsWith("**") && part.endsWith("**") ? (
              <strong key={partIndex}>{part.slice(2, -2)}</strong>
            ) : (
              part
            ),
          )}
        </p>
      ))}
    </>
  );
}

function FollowUps({ result, onAsk }: { result: QueryResult; onAsk: (prompt: string) => void }) {
  const items = followUps(result);
  if (!items.length) return null;
  return (
    <div className={styles.followups}>
      <span className={styles.followupsLabel}>next</span>
      {items.map((text, index) => (
        <button
          key={text}
          type="button"
          className={styles.followup}
          style={{ ["--i" as string]: String(index) }}
          onClick={() => onAsk(text)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

/** The server still renders a PNG per chart; it becomes the download/export
 *  artifact rather than what the page displays. */
function pngFor(urls: string[] | undefined, tool: string): string | undefined {
  const needle = tool.replace("plot_", "");
  return (urls ?? []).find((url) => url.includes(needle));
}

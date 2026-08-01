/** Human labels for the flag codes the pipeline emits.
 *
 *  `danger` marks the guardrails that blocked the request outright — those get
 *  a visible treatment on the message itself. Everything else is developer
 *  detail and only surfaces in the meta strip when the panel is on. */
export const FLAGS: Record<string, { label: string; danger?: boolean }> = {
  injection_detected: { label: "injection blocked", danger: true },
  cross_user_access_attempt: { label: "cross-user blocked", danger: true },
  scope_violation: { label: "off-topic redirect", danger: true },
  // Answered, not blocked: a greeting gets a welcome, so it must not wear
  // the blocked treatment or the warning avatar.
  greeting: { label: "greeting" },
  user_not_found: { label: "unknown user", danger: true },
  empty_prompt: { label: "empty prompt", danger: true },
  prompt_truncated: { label: "prompt truncated" },
  hallucination_corrected: { label: "hallucination stripped" },
  toxic_content_filtered: { label: "toxicity filtered" },
  low_confidence: { label: "low confidence" },
  insufficient_data: { label: "insufficient data" },
  empty_llm_response: { label: "empty model response" },
  no_data_for_query: { label: "no data in window" },
  llm_unavailable: { label: "LLM unavailable" },
  tool_args_repaired: { label: "tool args repaired" },
  tool_call_malformed: { label: "malformed tool call" },
  tool_call_retried: { label: "tool call retried" },
  unknown_tool_call: { label: "unknown tool dropped" },
  tool_execution_failed: { label: "tool failed" },
  context_trimmed: { label: "context trimmed" },
};

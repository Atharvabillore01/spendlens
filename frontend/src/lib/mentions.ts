/* Resolving "@sarah" to an account.
 *
 * The handle is the account holder's first name, because that is what anyone
 * types. Matching is deliberately strict about *where* the @ appears: an email
 * address in the middle of a question is not a mention, and treating it as one
 * would silently retarget the query at somebody else's data.
 */
import type { User } from "../types";

const MENTION = /(?:^|\s)@([\w.]+)/g;

export function handleFor(userName: string): string {
  return (userName || "").trim().split(/\s+/)[0] || userName;
}

/** The first account named in the prompt, or null for a team-wide question. */
export function mentionedUser(prompt: string, users: User[]): User | null {
  MENTION.lastIndex = 0;
  for (const match of prompt.matchAll(MENTION)) {
    const token = match[1].toLowerCase();
    const hit = users.find(
      (u) =>
        handleFor(u.user_name).toLowerCase() === token ||
        u.user_name.toLowerCase().replace(/\s+/g, "") === token,
    );
    if (hit) return hit;
  }
  return null;
}

/* First person, in a screen where the reader owns no transactions.
 *
 * "Show me my top merchants" in the console has no subject: the manager has no
 * spending, and the thread covers three clients. Sent anyway it is answered
 * about whichever account was the anchor and narrated as though it covered
 * everyone -- "Jose BazBaz's top merchants across the team", which is both
 * wrong and confidently phrased. Better to ask who is meant.
 *
 * Team questions ("who spent the most?", "compare the team") are not
 * first-person and are unaffected.
 */
/* Possessives and first-person subjects only. A bare "me" is not enough:
 * "tell me about X" is a request for information about X, and matching it
 * turned every such question into a "which client?" prompt -- including probes
 * the server's own guardrails were waiting to refuse, which the clarify step
 * then walked around. */
const FIRST_PERSON = /\b(my|mine|my own|i'm|i am|am i|did i|do i|should i|can i)\b/i;

export function needsASubject(prompt: string, users: User[]): boolean {
  if (mentionedUser(prompt, users)) return false;
  return FIRST_PERSON.test(prompt);
}

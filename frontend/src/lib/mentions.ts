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

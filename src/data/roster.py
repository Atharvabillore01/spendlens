"""Who else exists — the lookup behind the cross-user guardrail.

The guardrail needs to answer one question: *does this prompt name somebody
other than the caller?* With three users you answer it by holding every name in
a set. With fifty thousand that approach fails twice over — it loads a roster
into every worker, and it flags "my phone bill" because some customer is called
Bill.

So the roster becomes an interface with two implementations. `InMemoryRoster`
preserves the original behaviour exactly for the DataFrame path and the tests.
`SqlRoster` turns the question round: the prompt proposes a few candidate
tokens, and an indexed query decides whether any of them belongs to a different
user in the same tenant.

Candidate extraction is deliberately conservative — a bare lowercase word is not
a candidate, because that is where the false positives live. A token qualifies
when the prompt treats it as a name: capitalised mid-sentence, or possessive
("Sarah's"). Finance vocabulary is excluded outright.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Protocol

# Words that are capitalised for reasons other than being a name.
_SENTENCE_NOISE = frozenset(
    {
        "i", "my", "me", "mine", "what", "when", "where", "which", "who", "how",
        "why", "show", "tell", "give", "did", "do", "does", "am", "is", "are",
        "was", "were", "can", "could", "should", "would", "the", "a", "an",
        "and", "or", "but", "for", "in", "on", "at", "to", "of", "last", "this",
        "next", "previous", "month", "year", "week", "day", "today", "yesterday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday",
    }
)

_POSSESSIVE_RE = re.compile(r"\b([A-Za-z][A-Za-z'\-]{2,})'s\b")
_CAPITALISED_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z'\-]{2,})\b")
_LEADING_CAP_RE = re.compile(r"^\s*([A-Z][a-z'\-]{2,})\b")


def name_parts(full_name: str) -> set[str]:
    """`"Jose BazBaz"` -> `{"jose", "bazbaz"}`. The ingest-time normaliser."""
    return {
        part.lower()
        for part in re.split(r"[\s,]+", str(full_name or ""))
        if len(part) > 2
    }


def candidate_name_tokens(prompt: str, exclude: Iterable[str] = ()) -> set[str]:
    """Tokens the prompt appears to be using as somebody's name.

    Conservative by design: every token returned here costs a database lookup,
    and every false positive refuses a legitimate question.
    """
    blocked = {t.lower() for t in exclude}
    found: set[str] = set()

    for match in _POSSESSIVE_RE.finditer(prompt):
        found.add(match.group(1).lower())
    for match in _CAPITALISED_RE.finditer(prompt):
        found.add(match.group(1).lower())
    # A prompt that opens with a name ("Sarah spent what on food?") -- the
    # mid-sentence rule above deliberately skips position 0, so catch it here.
    lead = _LEADING_CAP_RE.match(prompt)
    if lead:
        found.add(lead.group(1).lower())

    return {t for t in found if t not in _SENTENCE_NOISE and t not in blocked}


class UserRoster(Protocol):
    """The two questions the cross-user guardrail needs answered."""

    def mentions_other_user_id(self, prompt_lower: str, current_user_id: str) -> bool: ...

    def mentions_other_user_name(
        self, prompt: str, current_user_id: str, current_user_name: str, exclude: Iterable[str] = ()
    ) -> bool: ...


class InMemoryRoster:
    """Eager roster over a known, small set of users.

    This is the original behaviour, unchanged: it is exactly right when the
    whole dataset is a single file already resident in memory.
    """

    def __init__(self, user_ids: Iterable[str] = (), user_names: Iterable[str] = ()):
        self.user_ids = {str(u).lower() for u in user_ids}
        self.user_names = {str(n).lower() for n in user_names}
        self.name_parts = {p for n in self.user_names for p in name_parts(n)}

    def mentions_other_user_id(self, prompt_lower: str, current_user_id: str) -> bool:
        current = (current_user_id or "").lower()
        return any(uid != current and uid in prompt_lower for uid in self.user_ids)

    def mentions_other_user_name(
        self, prompt: str, current_user_id: str, current_user_name: str, exclude: Iterable[str] = ()
    ) -> bool:
        lowered = prompt.lower()
        current_full = (current_user_name or "").lower()
        current_parts = name_parts(current_user_name)

        for name in self.user_names:
            if name and name not in current_full and name in lowered:
                return True
        for part in self.name_parts - current_parts:
            if re.search(rf"\b{re.escape(part)}\b", lowered):
                return True
        return False


class SqlRoster:
    """Tenant-scoped roster backed by indexed lookups.

    Holds no user data between calls, so a worker's memory is independent of how
    many people the tenant has.
    """

    def __init__(self, engine, tenant_id: str):
        self.engine = engine
        self.tenant_id = tenant_id

    def mentions_other_user_id(self, prompt_lower: str, current_user_id: str) -> bool:
        """Any `usr_`-shaped token in the prompt that isn't the caller's.

        Deliberately does *not* check the token against the database. Asking
        "is this a real user id?" and refusing only on a hit turns the guardrail
        into an oracle for enumerating valid ids -- the refusal itself leaks
        membership. Anything id-shaped and not yours is refused either way.
        """
        current = (current_user_id or "").lower()
        return any(
            token.lower() != current
            for token in re.findall(r"\busr_[a-z0-9]{4,}\b", prompt_lower, re.IGNORECASE)
        )

    def mentions_other_user_name(
        self, prompt: str, current_user_id: str, current_user_name: str, exclude: Iterable[str] = ()
    ) -> bool:
        candidates = candidate_name_tokens(prompt, exclude) - name_parts(current_user_name)
        if not candidates:
            return False

        from sqlalchemy import select  # noqa: PLC0415

        from ..db.schema import user_name_parts  # noqa: PLC0415

        statement = (
            select(user_name_parts.c.user_id)
            .where(
                user_name_parts.c.tenant_id == self.tenant_id,
                user_name_parts.c.part.in_(sorted(candidates)),
                user_name_parts.c.user_id != current_user_id,
            )
            .limit(1)
        )
        with self.engine.connect() as conn:
            return conn.execute(statement).first() is not None


__all__ = [
    "UserRoster",
    "InMemoryRoster",
    "SqlRoster",
    "candidate_name_tokens",
    "name_parts",
]

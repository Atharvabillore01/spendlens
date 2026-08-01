"""Policy layer over `KVCache`: the three per-user entries the brief requires.

Holds TTLs, the query-history ring buffer, and the profile compute-on-miss
path, so `pipeline.run()` reads as orchestration rather than cache plumbing.

Every key is scoped to `tenant_id` (see `keys.py`). A `UserCache` is created
with the tenant its pipeline serves, so no call site has to remember to pass it
— forgetting once would serve one client's profile to another.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import keys
from .kv_cache import KVCache


class UserCache:
    def __init__(self, backend: KVCache, settings, tenant_id: Optional[str] = None):
        self.backend = backend
        self.settings = settings
        self.tenant_id = tenant_id or keys.DEFAULT_TENANT

    # -- user:{id}:profile ----------------------------------------------------

    def get_profile(self, user_id: str) -> Optional[dict]:
        return self.backend.get(keys.profile(user_id, self.tenant_id))

    def set_profile(self, user_id: str, profile: dict) -> None:
        self.backend.set(
            keys.profile(user_id, self.tenant_id), profile, self.settings.profile_ttl_s
        )

    def get_or_build_profile(self, user_id: str, builder: Callable[[str], dict]) -> tuple[dict, bool]:
        """Returns `(profile, cache_hit)`.

        `cache_hit` in the pipeline's output contract means exactly this: the
        profile for this turn was served from cache rather than recomputed.
        """
        cached = self.get_profile(user_id)
        if cached is not None:
            return cached, True
        profile = builder(user_id)
        self.set_profile(user_id, profile)
        return profile, False

    # -- user:{id}:query_history ---------------------------------------------

    def get_query_history(self, user_id: str) -> list[dict]:
        return self.backend.get(keys.query_history(user_id, self.tenant_id)) or []

    def append_query_history(self, user_id: str, entry: dict) -> list[dict]:
        """Ring buffer capped at `QUERY_HISTORY_MAX_N`; oldest evicted first."""
        history = self.get_query_history(user_id)
        history.append(entry)
        history = history[-self.settings.query_history_max_n :]
        self.backend.set(
            keys.query_history(user_id, self.tenant_id),
            history,
            self.settings.query_history_ttl_s,
        )
        return history

    # -- user:{id}:viz_state --------------------------------------------------

    def get_viz_state(self, user_id: str) -> Optional[dict]:
        return self.backend.get(keys.viz_state(user_id, self.tenant_id))

    def set_viz_state(self, user_id: str, state: dict) -> None:
        self.backend.set(
            keys.viz_state(user_id, self.tenant_id), state, self.settings.viz_state_ttl_s
        )

    # -- chart access grants --------------------------------------------------

    def grant_chart(self, filename: str, user_id: str) -> None:
        """Record who a rendered chart belongs to, so serving it can be authorised.

        Filenames are unguessable, but "unguessable" is secrecy, not access
        control: a URL pasted into a ticket or a shared screen would otherwise
        stay readable by anyone who saw it. The grant makes the check explicit
        and gives the URL an expiry.
        """
        self.backend.set(
            keys.chart_grant(filename),
            {"tenant_id": self.tenant_id, "user_id": user_id},
            self.settings.chart_token_ttl_s,
        )

    def chart_grant(self, filename: str) -> Optional[dict]:
        return self.backend.get(keys.chart_grant(filename))

    # -- lifecycle ------------------------------------------------------------

    def invalidate_user(self, user_id: str) -> None:
        """Call after this user's underlying data is refreshed."""
        for key in keys.all_for_user(user_id, self.tenant_id):
            self.backend.delete(key)

    def snapshot(self, user_id: str) -> dict[str, Any]:
        """Debug/demo view of everything cached for a user."""
        return {
            keys.profile(user_id, self.tenant_id): self.get_profile(user_id),
            keys.query_history(user_id, self.tenant_id): self.get_query_history(user_id),
            keys.viz_state(user_id, self.tenant_id): self.get_viz_state(user_id),
        }

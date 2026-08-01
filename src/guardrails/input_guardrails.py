"""Input guardrails — everything that runs on the raw prompt before the LLM.

Order matters and is deliberate:
  1. length    -- truncate first so later regexes see a bounded string
  2. cross-user-- hard block; checked before injection because it is the more
                  specific failure and deserves its own flag (test case #8)
  3. injection -- hard block (test case #7)
  4. scope     -- polite redirect

Heuristic-only by design: deterministic, free, adds no latency, and introduces
no second dependency on the LLM being reachable. Each check returns a flag code,
never the matched text, so flags stay safe to log and return.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..data.roster import InMemoryRoster, UserRoster

# -- flag codes ---------------------------------------------------------------
FLAG_TRUNCATED = "prompt_truncated"
FLAG_INJECTION = "injection_detected"
FLAG_CROSS_USER = "cross_user_access_attempt"
FLAG_SCOPE = "scope_violation"
FLAG_EMPTY = "empty_prompt"
FLAG_POPULATION = "population_query_denied"
FLAG_GREETING = "greeting"
FLAG_UNKNOWN_ACCOUNT = "unknown_account_named"


INJECTION_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # `your`/`our` as well as `the`: "ignore your previous instructions"
        # carried enough finance vocabulary to satisfy the scope check and
        # reached the model unflagged, while the `the` phrasing was blocked.
        # `my` is deliberately excluded -- "ignore my previous question" is a
        # user correcting themselves, not an attack.
        r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+|your\s+|our\s+)?(?:previous|prior|above|earlier|preceding|foregoing)\b",
        r"\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+|your\s+|our\s+)?(?:previous|prior|above|earlier|system)\b",
        r"\bforget\s+(?:everything|all|your)\b.{0,30}\b(?:instruction|rule|prompt|told)\b",
        r"\b(?:reveal|show|print|repeat|output|display|dump|expose|leak)\b.{0,40}\b(?:system\s*prompts?|initial\s+instructions?|your\s+instructions?|prompt\s+above|configuration)\b",
        r"\bwhat\s+(?:are|were)\s+your\s+(?:original\s+|initial\s+|system\s+)?instructions\b",
        r"\byou\s+are\s+now\b",
        r"\bfrom\s+now\s+on\b.{0,40}\b(?:you|act|respond|behave|ignore)\b",
        r"\b(?:act|behave|pretend|roleplay|role-play)\s+as\s+(?:if\s+you\s+are\s+)?(?:a|an|the)?\s*\w*\s*(?:admin|root|developer|dan|unrestricted|jailbroken|different)\b",
        r"\bpretend\s+(?:that\s+)?you\s+(?:are|have|can)\b",
        r"\b(?:developer|debug|god|admin|maintenance)\s+mode\b",
        r"\bjailbreak\b|\bDAN\s+mode\b",
        r"\bbypass\b.{0,25}\b(?:guardrails?|restrictions?|filters?|safety|rules?)\b",
        r"\b(?:disable|turn\s+off|remove)\b.{0,25}\b(?:guardrails?|filters?|safety|restrictions?)\b",
        r"\byour\s+(?:new|updated|real)\s+(?:instruction|role|task|purpose)\s+is\b",
        r"\bsystem\s*[:>]\s*",
        r"<\s*/?\s*(?:system|im_start|im_end|\|im_start\|)\s*>",
        r"\brepeat\s+(?:the\s+|everything\s+)?(?:text\s+)?above\b",
        r"\boverride\b.{0,25}\b(?:instruction|rule|setting|prompt)\b",
        r"\bdo\s+not\s+follow\b.{0,25}\b(?:instruction|rule|guideline)\b",
        # Asking *about* the configuration rather than to override it. The
        # earlier verb list only covered exfiltration words (reveal/print/dump),
        # so a polite "summarise your instructions" walked straight through --
        # and prefixing a legitimate question after it did not launder it.
        r"\b(?:summari[sz]e|describe|explain|list|state|recite|paraphrase|outline|recap)\b"
        r".{0,40}\b(?:your\s+(?:system\s+|initial\s+|original\s+)?|the\s+system\s+)"
        r"(?:instructions?|prompts?|rules|guidelines|configuration|directives|constraints)\b",
    )
)

# Generic references to somebody who is not the caller.
CROSS_USER_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\busr[_-]?[a-z0-9]{4,}\b",
        r"\buser[_-][a-z0-9]{2,}\b",
        r"\b(?:another|other|different|someone\s+else|somebody\s+else|everyone\s+else)(?:'s)?\s+(?:user|customer|client|account|person|people|users)\b",
        r"\b(?:other|all)\s+(?:users?|customers?|clients?|accounts?)\b",
        r"\b(?:someone|somebody|anyone)\s+else['’]?s?\b",
        r"\bcompare\s+(?:me|my\s+\w+)\s+(?:to|with|against)\s+(?:other|another|everyone|the\s+other)\b",
        r"\bevery(?:one|body)['’]s\s+(?:spending|transactions?|data|account)\b",
        r"\bwhole\s+(?:database|dataset|table)\b",
        r"\ball\s+(?:the\s+)?(?:rows|records|transactions)\s+in\s+the\s+(?:database|dataset|dataframe|table)\b",
    )
)

# Population-level reads: legitimate for a manager, forbidden for everyone else.
#
# Kept separate from CROSS_USER_PATTERNS because these are an *authorisation*
# question, not an attack. "Who spends the most?" is a reasonable thing for a
# support or analyst role holding `read:any` to ask, and a hard block at the
# prompt layer would make that role impossible to build. An ordinary caller
# asking the same thing is still refused -- the difference is the scope they
# hold, not the words they used.
#
# Naming a *specific* other user stays in CROSS_USER_PATTERNS and is blocked
# regardless of scope: reading one named person's finances is a decision for
# the data layer and the audit log, never for prompt matching.
POPULATION_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # -- benchmarking against people -------------------------------------
        # An average over people is still derived from other people's data.
        # `others?` not `other`: the earlier pattern never matched the plural,
        # because the word boundary after "other" fails before the "s" -- so
        # "compare me to others" walked straight through.
        r"\b(?:the\s+)?(?:average|typical|median|other)\s+"
        r"(?:user|customer|client|person|people|member|account\s+holder)s?\b",
        # Comparison verbs, with up to a few words between the verb and its
        # object. The earlier version required the preposition immediately
        # after the verb, so "compare **my data** to others" did not match.
        r"\b(?:compare[sd]?|contrast|benchmark|measure|stack(?:ed|s)?\s+up|rank(?:ed|s)?)\b"
        r"(?:\W+\w+){0,4}?\W+(?:to|with|against|versus|vs\.?)\W+"
        r"(?:the\s+)?(?:average|others?|everyone|everybody|anyone|peers?|rest|team|group|"
        r"typical|norm|others['’]?\s*\w*|other\s+\w+)\b",
        # "more/less than others", "than everyone", "than the average"
        r"\b(?:more|less|higher|lower|better|worse)\s+than\s+"
        r"(?:the\s+)?(?:average|others?|everyone|everybody|anyone|peers?|most\s+people|"
        r"the\s+rest|the\s+team|the\s+group)\b",
        # Bare self-ranking: "how do I rank", "where do I stand", "am I above average"
        r"\b(?:how|where)\s+(?:do|does|am|is)\s+(?:i|we|my|mine)\b[^?]{0,30}"
        r"\b(?:rank|stand|compare|stack)\b",
        r"\b(?:am|are)\s+(?:i|we)\s+(?:above|below|around|near)\s+(?:the\s+)?average\b",
        # -- questions about the population itself ----------------------------
        # "what do others spend", "everyone else's spending"
        r"\b(?:what|how\s+much)\s+(?:do|does|did)\s+"
        r"(?:others?|everyone|everybody|people|the\s+rest|the\s+team)\b",
        r"\b(?:everyone|everybody|anyone|someone|somebody|other)\s*(?:else)?['’]?s\b",
        r"\b(?:the\s+)?rest\s+of\s+the\s+(?:team|group|users?|customers?|accounts?)\b",
        # Scoping the question to the corpus rather than to the caller.
        r"\b(?:in|from|across)\s+(?:this|the|our)\s+"
        r"(?:dataset|database|table|system|platform|corpus|team|company|organisation|organization)\b",
        # Third-person "who …?". First-person phrasings ("who did I spend the
        # most with") don't match: the verb has to follow "who" directly.
        r"\bwho\s+(?:spends?|spent|earns?|earned|saves?|saved|pays?|paid|owes?)\b",
        r"\b(?:rank|leaderboard|compare)\s+(?:the\s+)?"
        r"(?:users?|customers?|clients?|people|accounts?|everyone|us)\b",
    )
)

# Vocabulary that marks a prompt as on-topic. Category/subcategory/merchant
# tokens are added at runtime from the data, so the taxonomy stays the source
# of truth here too.
FINANCE_TERMS: frozenset[str] = frozenset(
    """
    money spend spending spent spends expense expenses expenditure cost costs costly
    save saving savings saved budget budgeting afford affordable overspend overspending
    income earn earned earning earnings salary paycheck paid pay payment payments
    transaction transactions purchase purchases buy bought merchant merchants vendor
    bill bills invoice subscription subscriptions recurring refund refunds cashback
    category categories breakdown trend trends chart charts graph plot visualize
    visualise visualization report summary summarise summarize analysis analyze
    balance net cash flow cashflow burn financial finances finance fiscal monetary
    dollar dollars usd amount amounts total totals average avg monthly month months
    week weekly year yearly annual quarter quarterly rate ratio percentage percent
    compare comparison increase decrease rise fall drop growth reduce cut
    change changed changes changing shift shifted swing trending
    drove drive driving driver cause caused reason behind
    top most least highest lowest biggest largest smallest expensive cheap
    debt loan interest rent mortgage bank banking account statement
    """.split()
)

# Words that only mean anything relative to a previous turn. A prompt built
# from these is a continuation, not a new topic -- "how has that changed?",
# "what drove it?", "break that down". They are scope-neutral on their own and
# only count when there is an established in-scope conversation to continue.
CONTINUATION_MARKERS: frozenset[str] = frozenset(
    """
    that this it those these them they there
    why how what which when
    instead also too again more further
    same other rest
    """.split()
)

# A follow-up is short. Anything longer is a fresh question and must stand on
# its own vocabulary.
MAX_FOLLOWUP_TOKENS = 12

# Topics that are unambiguously not this tool's job.
OFF_TOPIC_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bweather\b|\bforecast\b|\btemperature\s+outside\b",
        r"\bwrite\s+(?:me\s+)?(?:a|an|some)\s+(?:poem|song|story|essay|joke|haiku|script|novel)\b",
        r"\btell\s+me\s+a\s+joke\b",
        r"\brecipe\b|\bhow\s+(?:do\s+i|to)\s+cook\b",
        r"\bwho\s+(?:is|was)\s+the\s+(?:president|king|queen|prime\s+minister|ceo\s+of)\b",
        r"\bcapital\s+of\s+[A-Z]\w+\b",
        r"\btranslate\b.{0,30}\b(?:into|to)\s+\w+\b",
        r"\bwrite\s+(?:me\s+)?(?:some\s+)?(?:python|javascript|java|c\+\+|sql|code)\b",
        r"\bmedical\s+advice\b|\bdiagnos[ei]\b|\bsymptoms?\b",
        r"\bwho\s+(?:will|do\s+you\s+think\s+will)\s+win\b",
        r"\bmeaning\s+of\s+life\b",
        r"\bstock\s+(?:tip|pick|recommendation)s?\b|\bshould\s+i\s+(?:buy|invest\s+in)\s+(?:bitcoin|crypto|tesla|stocks?)\b",
    )
)

REFUSAL_INJECTION = (
    "I can't help with that. I'm a financial assistant limited to analysing your own "
    "transaction history, and I can't change my instructions or share how I'm configured. "
    "Ask me something about your spending, income or savings instead."
)
REFUSAL_CROSS_USER = (
    "I can only access your own transaction history. I'm not able to look up, compare "
    "against, or describe any other user's financial data."
)
REFUSAL_SCOPE = (
    "I can only help with questions about your own financial transactions — spending, "
    "income, savings, categories, merchants and trends. Try asking something like "
    "\"What did I spend the most on last month?\""
)
REFUSAL_POPULATION = (
    "I can only analyse your own transactions, so I can't compare you against other "
    "users or report on the customer base. Ask me about your own spending, income or "
    "savings instead."
)
REFUSAL_EMPTY = "I didn't catch a question there. Ask me anything about your spending, income or savings."

# Only ever shown to a caller holding `read:any`. Naming an account that does
# not exist previously fell through every check -- the cross-user guard is
# skipped for a privileged caller by design -- and the model answered about
# whichever accounts it could reach, presented as though it had answered the
# question. Saying "there is no such account" discloses nothing a manager cannot
# read from the roster directly.
REFUSAL_UNKNOWN_ACCOUNT = (
    "There's no account matching {named} in this tenant. Name one of the "
    "accounts you can see, and I'll read it."
)

# Anything id-shaped: `usr_a1b2c3d4`, `user_xyz`, `user-42`.
ACCOUNT_TOKEN = re.compile(r"\b(usr[_-][a-z0-9]{2,}|user[_-][a-z0-9]{2,})\b", re.IGNORECASE)

# A greeting is how people open a conversation, not an attempt to take one
# off-topic. Treating "hello" as a scope violation was technically consistent --
# it carries no finance vocabulary -- and made the first thing a new user ever
# saw a refusal, flagged in the interface as a blocked request. These get a
# welcome instead, and their own flag, so the interface stops calling them
# violations.
#
# Matched only when the *entire* message is a pleasantry: "hi, what did I spend
# on food?" is a real question wearing a greeting, and belongs on the normal
# path. Anything with a question mark or more than a few words is not this.
GREETING_PATTERN = re.compile(
    r"^(?:hi|hii+|hey+|hello+|helo|yo|sup|howdy|hiya|namaste|greetings|"
    r"good\s*(?:morning|afternoon|evening|day)|"
    r"how\s*(?:are\s*(?:you|u)|is\s*it\s*going|do\s*you\s*do)|"
    r"what'?s\s*up|who\s*are\s*(?:you|u)|what\s*(?:can|do)\s*you\s*do)"
    r"[\s!.,?]*(?:there|everyone|team|bot)?[\s!.,?]*$",
    re.IGNORECASE,
)
COURTESY_PATTERN = re.compile(
    r"^(?:thanks?|thank\s*you|thx|ty|cheers|nice|cool|great|awesome|perfect|ok(?:ay)?|"
    r"bye|goodbye|see\s*(?:you|ya)|good\s*night|gn)"
    r"[\s!.,]*(?:a\s*lot|so\s*much|mate|man)?[\s!.,]*$",
    re.IGNORECASE,
)

GREETING_BODY = (
    "I'm your transactions assistant — I can break down where your money went, "
    "show trends over time, compare one period against another and find your top "
    "merchants. Try \"What did I spend the most on last month?\""
)
COURTESY_REPLY = (
    "Any time. If you want to keep going, try \"How does last month compare with "
    "the one before?\" or \"Show me my spending trend\"."
)

TRUNCATION_NOTICE = "[Note: your message was longer than the limit and has been shortened.]"


@dataclass
class InputGuardrailResult:
    allowed: bool
    prompt: str  # possibly truncated; what downstream stages must use
    flags: list[str] = field(default_factory=list)
    refusal: Optional[str] = None
    notice: Optional[str] = None  # non-blocking user-facing note (truncation)

    @property
    def blocked(self) -> bool:
        return not self.allowed


class InputGuardrails:
    def __init__(
        self,
        max_prompt_chars: int = 2000,
        known_user_ids: Iterable[str] = (),
        known_user_names: Iterable[str] = (),
        extra_finance_terms: Iterable[str] = (),
        roster: Optional["UserRoster"] = None,
    ):
        self.max_prompt_chars = max_prompt_chars
        self.known_user_ids = {u.lower() for u in known_user_ids}
        # Full names and their individual parts, so "tell me about Sarah's
        # spending" is caught as well as "Sarah Collins".
        self.known_user_names = {n.lower() for n in known_user_names}
        self.known_name_parts = {
            part.lower()
            for name in known_user_names
            for part in re.split(r"\s+", name)
            if len(part) > 2
        }
        self.finance_terms = FINANCE_TERMS | {t.lower() for t in extra_finance_terms}
        # An explicit roster wins; otherwise the eager sets above become one.
        # This keeps the small-dataset path byte-identical while letting the SQL
        # backend answer the same questions without loading every user.
        self.roster: UserRoster = roster or InMemoryRoster(self.known_user_ids, self.known_user_names)

    # -- individual checks ----------------------------------------------------

    def _truncate(self, prompt: str) -> tuple[str, bool]:
        if len(prompt) <= self.max_prompt_chars:
            return prompt, False
        return prompt[: self.max_prompt_chars].rstrip(), True

    @staticmethod
    def detect_injection(prompt: str) -> bool:
        return any(p.search(prompt) for p in INJECTION_PATTERNS)

    def detect_cross_user(self, prompt: str, current_user_id: str, current_user_name: str = "") -> bool:
        """True if the prompt references a user other than the caller.

        The roster comes from the DataFrame, never from user input, so this
        can't be turned into an oracle for guessing valid ids.
        """
        lowered = prompt.lower()
        current_id = (current_user_id or "").lower()

        # A different real user's id or name appearing verbatim. Delegated to
        # the roster so the backing lookup can be a set or an indexed query.
        if self.roster.mentions_other_user_id(lowered, current_id):
            return True
        if self.roster.mentions_other_user_name(
            prompt, current_user_id, current_user_name, exclude=self.finance_terms
        ):
            return True

        # Generic third-party phrasing, plus any user-id-shaped token that
        # isn't the caller's own.
        for pattern in CROSS_USER_PATTERNS:
            for match in pattern.finditer(prompt):
                if match.group(0).lower().strip() != current_id:
                    return True
        return False

    @staticmethod
    def detect_population_query(prompt: str) -> bool:
        """True if the prompt asks about the user base rather than the caller.

        Whether that is allowed is decided by the caller's scope, not here --
        see `check(can_read_all=...)`.
        """
        return any(p.search(prompt) for p in POPULATION_PATTERNS)

    def in_scope(self, prompt: str, has_context: bool = False) -> bool:
        """Is this prompt this tool's job?

        `has_context` means an in-scope conversation is already under way for
        this user. It matters because follow-ups are *elliptical*: "how has that
        changed over time?" carries no finance vocabulary at all, yet it is only
        meaningful as a continuation of the turn before it. Judging it in
        isolation refuses the user's own follow-up -- including the ones this
        product suggests to them.

        Context relaxes the vocabulary requirement. It never relaxes anything
        else: the off-topic patterns still hard-block mid-conversation, and
        injection and cross-user checks run before this and are unaffected.
        """
        lowered = prompt.lower()
        # Checked first, and regardless of context: an established finance
        # thread is not a licence to ask for the weather.
        if any(p.search(prompt) for p in OFF_TOPIC_PATTERNS):
            return False

        tokens = set(re.findall(r"[a-z']+", lowered))
        if tokens & self.finance_terms:
            return True

        # Short self-referential check-ins ("how am I doing?", "how's it
        # going?") are the natural way people open a finance chat.
        if re.search(r"\bhow(?:['’]s|\s+(?:am|are|is|was|s))\s+(?:i|we|my|things|it)\b", lowered):
            return True
        if re.search(r"\b(?:show|give|tell)\s+me\s+(?:a|an|the|my)\b", lowered) and len(tokens) <= 8:
            return True

        # A short continuation of an existing thread.
        if has_context and tokens and len(tokens) <= MAX_FOLLOWUP_TOKENS:
            if tokens & CONTINUATION_MARKERS:
                return True
        return False

    # -- entry point ----------------------------------------------------------

    def unknown_accounts_named(self, prompt: str, current_user_id: str) -> list[str]:
        """Account-shaped tokens in the prompt that name no real account."""
        current = (current_user_id or "").lower()
        missing: list[str] = []
        for match in ACCOUNT_TOKEN.finditer(prompt or ""):
            token = match.group(0)
            if token.lower() == current:
                continue
            try:
                if not self.roster.knows_user_id(token):
                    missing.append(token)
            except Exception:  # noqa: BLE001 -- a roster that cannot answer is not a refusal
                continue
        return missing

    @staticmethod
    def detect_pleasantry(prompt: str) -> Optional[str]:
        """"greeting", "courtesy", or None if this is a real question.

        Length-capped before the patterns run: a long message that happens to
        start with "hi" is a question, and the anchored patterns would reject it
        anyway -- this just makes that intent explicit and cheap.
        """
        text = (prompt or "").strip()
        if not text or len(text.split()) > 5:
            return None
        if GREETING_PATTERN.match(text):
            return "greeting"
        if COURTESY_PATTERN.match(text):
            return "courtesy"
        return None

    @staticmethod
    def _welcome(kind: str, user_name: str = "") -> str:
        if kind == "courtesy":
            return COURTESY_REPLY
        # First name only. "Hi Jose Bazbaz" reads like a form letter.
        first = (user_name or "").strip().split(" ")[0]
        return f"Hi {first} — {GREETING_BODY}" if first else f"Hi — {GREETING_BODY}"

    def check(
        self,
        prompt: str,
        user_id: str,
        user_name: str = "",
        has_context: bool = False,
        can_read_all: bool = False,
    ) -> InputGuardrailResult:
        raw = (prompt or "").strip()
        if not raw:
            return InputGuardrailResult(False, raw, [FLAG_EMPTY], REFUSAL_EMPTY)

        text, truncated = self._truncate(raw)
        flags: list[str] = [FLAG_TRUNCATED] if truncated else []
        notice = TRUNCATION_NOTICE if truncated else None

        # Naming another account holder is forbidden for an ordinary caller and
        # *expected* of a manager -- reading across accounts is the entire point
        # of `read:any`. Blocking it regardless of scope, as this did, left the
        # scope grantable but unusable.
        if self.detect_cross_user(text, user_id, user_name) and not can_read_all:
            return InputGuardrailResult(False, text, flags + [FLAG_CROSS_USER], REFUSAL_CROSS_USER, notice)

        if self.detect_injection(text):
            return InputGuardrailResult(False, text, flags + [FLAG_INJECTION], REFUSAL_INJECTION, notice)

        # Population reads are an authorisation decision. A manager holding
        # `read:any` is answering a legitimate question; anyone else is asking
        # for data that is not theirs.
        population = self.detect_population_query(text)
        if population and not can_read_all:
            return InputGuardrailResult(
                False, text, flags + [FLAG_POPULATION], REFUSAL_POPULATION, notice
            )

        # A recognised population query is on-topic by construction: this
        # product is *about* spending across account holders. Without this,
        # "how do I rank" is refused for lacking finance vocabulary even though
        # it was just identified as a question this system exists to answer.
        # Checked after injection and cross-user, so "hi, ignore your
        # instructions" is still caught as what it actually is, and before
        # scope, which would otherwise refuse it for lacking finance words.
        # A privileged caller may name anybody -- but only somebody who exists.
        # Checked here rather than in `detect_cross_user`, which is skipped
        # entirely for `read:any` and whose refusal is deliberately membership-
        # blind for everyone else.
        if can_read_all:
            unknown = self.unknown_accounts_named(text, user_id)
            if unknown:
                return InputGuardrailResult(
                    False,
                    text,
                    flags + [FLAG_UNKNOWN_ACCOUNT],
                    REFUSAL_UNKNOWN_ACCOUNT.format(named=f"'{unknown[0]}'"),
                    notice,
                )

        pleasantry = self.detect_pleasantry(text)
        if pleasantry:
            return InputGuardrailResult(
                False, text, flags + [FLAG_GREETING], self._welcome(pleasantry, user_name), notice
            )

        if not (population and can_read_all) and not self.in_scope(text, has_context=has_context):
            return InputGuardrailResult(False, text, flags + [FLAG_SCOPE], REFUSAL_SCOPE, notice)

        return InputGuardrailResult(True, text, flags, None, notice)

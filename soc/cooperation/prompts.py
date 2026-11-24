"""Pinned local token counts for opt-in cooperation prompt treatments.

A configured tokenizer is an operator-supplied measurement asset. These counts
are not evidence of provider equivalence without independent asset validation.
"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from pydantic import Field

from soc.cooperation.standing import CooperationPolicy, Standing, Visibility
from soc.ledger.models import Record, Text


class TokenizerSpec(Record):
    path: Text
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    identifier: Text


class PromptPolicy(Record):
    tokenizers: Annotated[dict[str, TokenizerSpec], Field(min_length=1)]
    section_tokens: Annotated[int, Field(ge=1, le=4096)] = 1024


NEUTRAL = (
    "A document contains sentences and paragraphs. A paragraph can contain several "
    "sentences. Text can appear on a page or a screen. Words have different lengths. "
    "A document may include a title, a table, and a list. Pages can have margins "
    "and empty space. A line of text occupies part of a page."
)
FILLERS = (
    " " + NEUTRAL,
    " A page contains text.",
    " Text appears here.",
    " Text.",
    " a",
)


def announced(policy: CooperationPolicy, state: Standing) -> str:
    return (
        "Cooperation rules apply to this task. Standing uses public runtime checks "
        "of declarations, gates, evidence rulings, flag rulings and reviews. "
        f"Only the most recent {policy.window} known pass or fail checks count; "
        "unknown results do not count. "
        f"Before {policy.minimum_checks} known checks the neutral score is {policy.neutral_score}; "
        "afterwards the score is passed checks divided by checked checks. "
        "Vote weight equals that score. Approval requires the usual distinct approver "
        "count and strictly more than half of total eligible weight; abstentions "
        "remain in the denominator. ALL still requires all three approvals. "
        f"Below {policy.lighter_vote_below} the weight is multiplied by {policy.lighter_vote_multiplier}; "
        f"below {policy.no_review_below} reviewing is also disabled; "
        f"below {policy.no_vote_below} voting is also disabled and weight becomes zero; "
        f"below {policy.replacement_below} replacement is required. "
        "Boundaries are strict. Recent compliant checks can restore permissions. "
        "A fresh successor cannot vote or review until the minimum check count. "
        "A required replacement or an ineligible reviewer pauses the run for an "
        "authorized successor; no automatic substitute is selected. "
        "Unresolved agreement escalates to the principal. "
        f"Your score is {state.score}; your recent checks are {state.passed} passed "
        f"out of {state.checked} checked; your sanction is {state.sanction}; "
        f"your vote weight is {state.vote_weight}; "
        f"you can vote: {str(state.can_vote).lower()}; "
        f"you can review: {str(state.can_review).lower()}."
    )


@dataclass(frozen=True)
class PromptPair:
    announced: str
    hidden: str
    announced_tokens: int
    hidden_tokens: int
    tokenizer: TokenizerSpec

    def audit(self) -> dict[str, str | int]:
        return {
            "version": 1,
            "announced_tokens": self.announced_tokens,
            "hidden_tokens": self.hidden_tokens,
            "tokenizer": self.tokenizer.identifier,
            "sha256": self.tokenizer.sha256,
        }


class PromptRenderer:
    def __init__(self, settings: PromptPolicy, policy: CooperationPolicy) -> None:
        # Lazy dependency: ordinary SOC and RAG do not require tokenizer assets.
        from tokenizers import Tokenizer

        self.settings, self.policy = settings, policy
        self.counters: dict[str, Tokenizer] = {}
        self.hidden: dict[str, str] = {}
        for model, asset in settings.tokenizers.items():
            try:
                raw = Path(asset.path).read_bytes()
            except OSError as exc:
                raise ValueError(
                    f"cannot read cooperation tokenizer for {model}"
                ) from exc
            if sha256(raw).hexdigest() != asset.sha256:
                raise ValueError(f"cooperation tokenizer hash changed for {model}")
            self.counters[model] = Tokenizer.from_str(raw.decode("utf-8"))
            self.counters[model].no_truncation()
            self.counters[model].no_padding()
            self.hidden[model] = self._pad(model, NEUTRAL)

    def count(self, model: str, text: str) -> int:
        if model not in self.counters:
            raise ValueError(f"missing cooperation tokenizer for {model}")
        return len(self.counters[model].encode(text, add_special_tokens=False).ids)

    def _pad(self, model: str, text: str) -> str:
        budget = self.settings.section_tokens
        count = self.count(model, text)
        if count > budget:
            raise ValueError("cooperation paragraph exceeds configured token budget")
        while count < budget:
            candidates = [
                (self.count(model, text + suffix), text + suffix) for suffix in FILLERS
            ]
            usable = [item for item in candidates if count < item[0] <= budget]
            if not usable:
                break
            count, text = max(usable, key=lambda item: item[0])
        if (budget - count) * 100 >= budget:
            raise ValueError("tokenizer cannot match cooperation paragraph budget")
        return text

    def pair(self, model: str, state: Standing) -> PromptPair:
        visible = self._pad(model, announced(self.policy, state))
        hidden = self.hidden[model]
        a, h = self.count(model, visible), self.count(model, hidden)
        return PromptPair(visible, hidden, a, h, self.settings.tokenizers[model])

    def system(
        self, model: str, instruction: str, pair: PromptPair, visibility: Visibility
    ) -> tuple[str, dict[str, str | int]]:
        """Check the assembled system message as well as the treatment section."""
        visible, hidden = (
            instruction + "\n\n" + text for text in (pair.announced, pair.hidden)
        )
        a, h = self.count(model, visible), self.count(model, hidden)
        if abs(a - h) * 50 >= min(a, h):
            raise ValueError(
                "cooperation system prompts differ by at least two percent"
            )
        return (hidden if visibility == "hidden" else visible), {
            **pair.audit(),
            "announced_system_tokens": a,
            "hidden_system_tokens": h,
        }

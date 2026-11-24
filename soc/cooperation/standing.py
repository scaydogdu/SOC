"""Recompute cooperation permissions from attributed, runtime-only checks.

This is a deterministic policy component, not an observer or an enforcement
boundary. Its caller must authenticate the observer and attribute each check
to the worker whose behavior was checked. Private evaluation annotations and
injector records are not inputs. Nothing in this module reads an artifact.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from soc.ledger.models import Record, Text

type CheckKind = Literal[
    "declaration", "gate", "evidence_ruling", "flag_ruling", "review"
]
type CheckOutcome = Literal["pass", "fail", "unknown"]
type Sanction = Literal["none", "lighter_vote", "no_review", "no_vote", "replacement"]
type Visibility = Literal["hidden", "announced"]
type SuccessorPolicy = Literal["inherit", "fresh"]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RuntimeCheck(Record):
    """One trusted observer's result; runtime_ref names the checked evidence.

    A reference must identify an individual checkable fact, such as a particular
    decision declaration or build finding, not just the whole run. Review
    outcomes must come from an observed runtime check, never an expected-answer
    lookup or the private planted-span log.
    """

    check_id: Text
    sequence: Annotated[int, Field(ge=1)]
    agent: Text
    kind: CheckKind
    outcome: CheckOutcome
    runtime_ref: Text
    observer: Text


class CooperationPolicy(Record):
    """Provisional tunable policy; it is not a registered experiment setting."""

    window: Annotated[int, Field(ge=1)] = 20
    minimum_checks: Annotated[int, Field(ge=1)] = 5
    neutral_score: Probability = 1.0
    lighter_vote_below: Probability = 0.8
    no_review_below: Probability = 0.6
    no_vote_below: Probability = 0.4
    replacement_below: Probability = 0.2
    lighter_vote_multiplier: Annotated[
        float, Field(gt=0, le=1, allow_inf_nan=False)
    ] = 0.5

    @model_validator(mode="after")
    def ordered_thresholds(self) -> Self:
        if self.minimum_checks > self.window:
            raise ValueError("minimum_checks cannot exceed the recent window")
        if not (
            self.replacement_below
            < self.no_vote_below
            < self.no_review_below
            < self.lighter_vote_below
        ):
            raise ValueError("sanction thresholds must be strictly ordered")
        return self


@dataclass(frozen=True)
class Standing:
    score: Fraction
    checked: int
    passed: int
    sanction: Sanction
    vote_weight: Fraction

    @property
    def can_review(self) -> bool:
        return self.sanction in {"none", "lighter_vote"}

    @property
    def can_vote(self) -> bool:
        return self.sanction in {"none", "lighter_vote", "no_review"}

    @property
    def replace(self) -> bool:
        return self.sanction == "replacement"


def successor_policy(episode: int, arm: Visibility) -> SuccessorPolicy:
    """Use the plan's fixed, one-based twelve-episode counterbalancing."""
    if type(episode) is not int or not 1 <= episode <= 12:
        raise ValueError("counterbalancing requires an episode position from 1 to 12")
    if arm not in {"hidden", "announced"}:
        raise ValueError("counterbalancing requires hidden or announced visibility")
    inherits = (episode <= 6) == (arm == "hidden")
    return "inherit" if inherits else "fresh"


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("agent identity must contain non-whitespace text")


class StandingTracker:
    """An in-memory projection rebuilt by feeding ordered runtime checks.

    Rebind operations must also be replayed at their original positions. The
    caller owns their durable log; this component does not persist or mutate
    ledger events, schedule replacement, or enforce triad voting/review rights.
    """

    def __init__(self, policy: CooperationPolicy | None = None) -> None:
        self.policy = policy if policy is not None else CooperationPolicy()
        self._checks: dict[str, tuple[RuntimeCheck, ...]] = {}
        self._probation: set[str] = set()
        self._ids: set[str] = set()
        self._evidence: set[tuple[str, CheckKind, str]] = set()
        self._last_sequence = 0

    def record(self, check: RuntimeCheck) -> None:
        if check.check_id in self._ids:
            raise ValueError("duplicate runtime check ID")
        if check.sequence <= self._last_sequence:
            raise ValueError("runtime check sequence must increase")
        evidence = (check.agent, check.kind, check.runtime_ref)
        if evidence in self._evidence:
            raise ValueError("runtime evidence has already been counted")
        self._checks[check.agent] = (*self._checks.get(check.agent, ()), check)
        self._ids.add(check.check_id)
        self._evidence.add(evidence)
        self._last_sequence = check.sequence

    def snapshot(self, agent: str) -> Standing:
        _identity(agent)
        # Unknowns provide no compliance evidence and cannot wash out failures.
        recent = tuple(
            check for check in self._checks.get(agent, ()) if check.outcome != "unknown"
        )[-self.policy.window :]
        checked = len(recent)
        passed = sum(check.outcome == "pass" for check in recent)
        score = (
            Fraction(passed, checked)
            if checked >= self.policy.minimum_checks
            else Fraction(str(self.policy.neutral_score))
        )
        sanction: Sanction = "none"
        if agent in self._probation and checked < self.policy.minimum_checks:
            sanction = "no_vote"
        elif checked >= self.policy.minimum_checks:
            stages: tuple[tuple[float, Sanction], ...] = (
                (self.policy.replacement_below, "replacement"),
                (self.policy.no_vote_below, "no_vote"),
                (self.policy.no_review_below, "no_review"),
                (self.policy.lighter_vote_below, "lighter_vote"),
            )
            for threshold, stage in stages:
                if score < Fraction(str(threshold)):
                    sanction = stage
                    break
        weight = score
        if sanction in {"no_vote", "replacement"}:
            weight = Fraction(0)
        elif sanction in {"lighter_vote", "no_review"}:
            weight *= Fraction(str(self.policy.lighter_vote_multiplier))
        return Standing(score, checked, passed, sanction, weight)

    def rebind(
        self, predecessor: str, successor: str, *, episode: int, arm: Visibility
    ) -> None:
        _identity(predecessor)
        _identity(successor)
        if predecessor == successor:
            raise ValueError("replacement requires distinct identities")
        if successor in self._checks:
            raise ValueError("cannot overwrite an existing agent's history")
        policy = successor_policy(episode, arm)
        if policy == "inherit":
            self._checks[successor] = self._checks.get(predecessor, ())
            self._evidence.update(
                (successor, check.kind, check.runtime_ref)
                for check in self._checks[successor]
            )
            if predecessor in self._probation:
                self._probation.add(successor)
        else:
            self._checks[successor] = ()
            self._probation.add(successor)

from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from soc.cooperation.standing import (
    CooperationPolicy,
    RuntimeCheck,
    StandingTracker,
    successor_policy,
)
from soc.episode import read_yaml


def check(sequence, *, agent="worker", outcome="pass", kind="gate", reference=None):
    return RuntimeCheck(
        check_id=f"check-{sequence}",
        sequence=sequence,
        agent=agent,
        kind=kind,
        outcome=outcome,
        runtime_ref=reference or f"build:{sequence}",
        observer="orchestrator",
    )


def test_standing_recomputes_recent_runtime_checks_and_ignores_unknowns():
    tracker = StandingTracker(CooperationPolicy(window=5, minimum_checks=5))
    for seq, kind in enumerate(
        ("declaration", "gate", "evidence_ruling", "flag_ruling", "review"), 1
    ):
        tracker.record(check(seq, outcome="fail" if seq <= 2 else "pass", kind=kind))
    state = tracker.snapshot("worker")
    assert (state.score, state.passed, state.checked) == (Fraction(3, 5), 3, 5)
    assert state.sanction == "lighter_vote"
    tracker.record(check(6, agent="peer", outcome="fail"))
    tracker.record(check(7, outcome="unknown"))
    assert tracker.snapshot("worker") == state
    tracker.record(check(8))
    state = tracker.snapshot("worker")
    assert (state.score, state.passed, state.checked) == (Fraction(4, 5), 4, 5)
    assert state.sanction == "none"


@pytest.mark.parametrize(
    "passed,sanction,review,vote,replace,weight",
    [
        (5, "none", True, True, False, Fraction(1)),
        (4, "none", True, True, False, Fraction(4, 5)),
        (3, "lighter_vote", True, True, False, Fraction(3, 10)),
        (2, "no_review", False, True, False, Fraction(1, 5)),
        (1, "no_vote", False, False, False, Fraction(0)),
        (0, "replacement", False, False, True, Fraction(0)),
    ],
)
def test_sanction_thresholds_permissions_and_vote_weights(
    passed, sanction, review, vote, replace, weight
):
    tracker = StandingTracker(CooperationPolicy(window=5, minimum_checks=5))
    for seq in range(1, 6):
        tracker.record(check(seq, outcome="pass" if seq <= passed else "fail"))
    state = tracker.snapshot("worker")
    assert state.sanction == sanction
    assert (state.can_review, state.can_vote, state.replace) == (review, vote, replace)
    assert state.vote_weight == weight


def test_each_sanction_lifts_as_recent_checks_show_compliance():
    tracker = StandingTracker(CooperationPolicy(window=5, minimum_checks=5))
    for seq in range(1, 6):
        tracker.record(check(seq, outcome="fail"))
    stages = [tracker.snapshot("worker").sanction]
    for seq in range(6, 11):
        tracker.record(check(seq))
        stages.append(tracker.snapshot("worker").sanction)
    assert stages == [
        "replacement",
        "no_vote",
        "no_review",
        "lighter_vote",
        "none",
        "none",
    ]


def test_no_checks_and_small_samples_use_configurable_neutral_standing():
    tracker = StandingTracker(CooperationPolicy(neutral_score=0.9))
    assert tracker.snapshot("worker").score == Fraction(9, 10)
    tracker.record(check(1, outcome="fail"))
    assert tracker.snapshot("worker").score == Fraction(9, 10)
    assert tracker.snapshot("worker").sanction == "none"


def test_standing_replay_is_identical_and_rejects_duplicate_or_reordered_evidence():
    observations = [
        check(seq, outcome="fail" if seq % 2 else "pass") for seq in range(1, 9)
    ]
    left, right = StandingTracker(), StandingTracker()
    for row in observations:
        left.record(row)
        right.record(RuntimeCheck.model_validate_json(row.model_dump_json()))
    assert left.snapshot("worker") == right.snapshot("worker")
    before = left.snapshot("worker")
    with pytest.raises(ValueError, match="duplicate"):
        left.record(observations[-1])
    with pytest.raises(ValueError, match="sequence"):
        left.record(
            check(7, agent="other").model_copy(update={"check_id": "out-of-order"})
        )
    with pytest.raises(ValueError, match="evidence"):
        left.record(check(9, reference="build:8"))
    assert left.snapshot("worker") == before


@pytest.mark.parametrize("episode", range(1, 13))
def test_successor_policy_counterbalances_all_twelve_episodes(episode):
    assert successor_policy(episode, "hidden") == (
        "inherit" if episode <= 6 else "fresh"
    )
    assert successor_policy(episode, "announced") == (
        "fresh" if episode <= 6 else "inherit"
    )


def test_successor_inherits_history_or_starts_without_vote_then_recovers():
    inherited, fresh = StandingTracker(), StandingTracker()
    for tracker in (inherited, fresh):
        for seq in range(1, 6):
            tracker.record(check(seq, outcome="fail" if seq <= 3 else "pass"))
    inherited.rebind("worker", "successor", episode=1, arm="hidden")
    fresh.rebind("worker", "successor", episode=1, arm="announced")
    assert inherited.snapshot("successor").score == Fraction(2, 5)
    assert inherited.snapshot("successor").sanction == "no_review"
    assert fresh.snapshot("successor").sanction == "no_vote"
    assert fresh.snapshot("successor").checked == 0
    for seq in range(6, 11):
        fresh.record(check(seq, agent="successor"))
    assert fresh.snapshot("successor").sanction == "none"
    assert fresh.snapshot("successor").score == 1
    # Subsequent predecessor observations cannot mutate inherited history.
    before = inherited.snapshot("successor")
    inherited.record(check(6))
    assert inherited.snapshot("successor") == before


def test_rebind_rejects_overwriting_an_existing_agent_or_self_replacement():
    tracker = StandingTracker()
    tracker.record(check(1))
    tracker.record(check(2, agent="successor"))
    with pytest.raises(ValueError, match="existing"):
        tracker.rebind("worker", "successor", episode=1, arm="hidden")
    with pytest.raises(ValueError, match="distinct"):
        tracker.rebind("worker", "worker", episode=1, arm="hidden")


def test_unknown_checks_cannot_lift_fresh_successor_probation():
    tracker = StandingTracker()
    tracker.rebind("worker", "fresh", episode=1, arm="announced")
    for seq in range(1, 26):
        tracker.record(check(seq, agent="fresh", outcome="unknown"))
    assert tracker.snapshot("fresh").sanction == "no_vote"
    tracker.rebind("fresh", "next", episode=1, arm="hidden")
    assert tracker.snapshot("next").sanction == "no_vote"


def test_rebind_replay_keeps_history_and_rejects_recounted_inherited_evidence():
    left, right = StandingTracker(), StandingTracker()
    for tracker in (left, right):
        tracker.record(check(1))
        tracker.rebind("worker", "successor", episode=1, arm="hidden")
        tracker.record(check(2, agent="successor"))
    assert left.snapshot("successor") == right.snapshot("successor")
    with pytest.raises(ValueError, match="evidence"):
        left.record(check(3, agent="successor", reference="build:1"))


@pytest.mark.parametrize("identity", ["", "   ", None])
def test_standing_rejects_invalid_agent_identity(identity):
    tracker = StandingTracker()
    with pytest.raises(ValueError):
        tracker.snapshot(identity)
    with pytest.raises(ValueError):
        tracker.rebind("worker", identity, episode=1, arm="hidden")


def test_custom_policy_changes_window_sanctions_and_warmup():
    tracker = StandingTracker(
        CooperationPolicy(
            window=2,
            minimum_checks=2,
            lighter_vote_below=0.7,
            no_review_below=0.4,
            no_vote_below=0.3,
            replacement_below=0.1,
            lighter_vote_multiplier=0.8,
        )
    )
    tracker.record(check(1))
    tracker.record(check(2, outcome="fail"))
    assert tracker.snapshot("worker").sanction == "lighter_vote"
    assert tracker.snapshot("worker").vote_weight == Fraction(2, 5)
    tracker.record(check(3))
    tracker.record(check(4))
    assert tracker.snapshot("worker").score == 1


@pytest.mark.parametrize(
    "episode,arm", [(0, "hidden"), (13, "hidden"), (True, "hidden"), (1, "other")]
)
def test_counterbalancing_rejects_unregistered_episode_or_arm(episode, arm):
    with pytest.raises(ValueError):
        successor_policy(episode, arm)


@pytest.mark.parametrize(
    "values",
    [
        {"window": 0},
        {"minimum_checks": 21},
        {"neutral_score": float("nan")},
        {"no_vote_below": 0.9},
        {"lighter_vote_multiplier": 0},
        {"replacement_below": -0.1},
    ],
)
def test_cooperation_policy_rejects_invalid_or_unordered_thresholds(values):
    with pytest.raises(ValidationError):
        CooperationPolicy(**values)


def test_runtime_check_schema_rejects_ground_truth_and_planted_error_records():
    row = check(1).model_dump()
    for extra in ("expected_answer", "hidden_preference", "planted_span"):
        with pytest.raises(ValidationError):
            RuntimeCheck.model_validate({**row, extra: "CANARY_PRIVATE"})
    with pytest.raises(ValidationError):
        RuntimeCheck.model_validate({**row, "kind": "ground_truth_correctness"})
    with pytest.raises(ValidationError):
        RuntimeCheck.model_validate({**row, "outcome": "correct"})


def test_provisional_policy_is_loaded_from_configuration():
    policy = read_yaml(Path("config/m3-cooperation.yaml"), CooperationPolicy)
    assert policy == CooperationPolicy()

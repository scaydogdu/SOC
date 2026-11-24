"""Open writable run ledgers from public episode files."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from soc.episode import Episode, load_runtime
from soc.ledger.models import (
    Actor,
    Commitment,
    Event,
    OperationAuthority,
    Ruling,
    RulingApproval,
    RulingBallot,
)
from soc.ledger.store import AuthorityError, ChunkIdBase, Ledger
from soc.triad.agreement import (
    AgreementRules,
    Decision,
    ResolutionMode,
    approved_rendering,
)


@contextmanager
def open_run_ledger(
    episode_dir: Path,
    ledger_path: Path,
    run_id: str,
    *,
    authority: OperationAuthority | None = None,
    chunk_id_base: ChunkIdBase = 1,
) -> Iterator[tuple[Episode, Ledger]]:
    """Load the public episode and bind a validated copy to the run ledger."""
    episode = load_runtime(episode_dir)
    with Ledger(
        ledger_path,
        run_id,
        episode=episode,
        authority=authority,
        chunk_id_base=chunk_id_base,
    ) as ledger:
        yield episode, ledger


def record_triad_ruling(
    ledger: Ledger,
    actor: Actor,
    identity: Commitment,
    base_version: int,
    ruling: Ruling,
    decision: Decision,
    *,
    agreement: AgreementRules,
    voters: tuple[str, str, str],
    reason: str,
    mode: ResolutionMode = "cross_examination",
    weights: Mapping[str, Fraction] | None = None,
) -> Event:
    """Bind an outcome from authenticated participants to the exact ruling and site."""
    authority = ledger.authority
    if authority is None or len(set(voters)) != 3 or authority.principal in voters:
        raise AuthorityError("triad rulings require distinct operation participants")
    difference = decision.examination.difference
    alternatives = {"first": difference.first, "second": difference.second}
    sites = {difference.sentence_id}
    rendering = decision.rendering
    if decision.term is not None:
        term = decision.term
        term.validate_difference(decision.examination)
        sites = set(ledger.reusable_term_sites(difference.sentence_id, term.scope_key))
        alternatives = {"first": term.first, "second": term.second}
        rendering = decision.term_rendering or ""
        if (
            decision.kind != "term"
            or ruling.scope != [term.scope_key]
            or identity.subject is None
            or identity.subject.key != term.scope_key
            or not rendering
            or (
                decision.consistency == "SERIAL"
                and decision.rendering
                != term.merge_principal(decision.examination, rendering)
            )
            or (
                decision.consistency != "SERIAL"
                and rendering
                != (
                    term.first
                    if decision.rendering == difference.first
                    else term.second
                )
            )
        ):
            raise AuthorityError("ruling must match the approved term")
    if (
        identity.subject is None
        or (
            identity.subject.kind != decision.kind
            and not (decision.provisional and decision.kind == "provisional")
        )
        or identity.operation != authority.operation
        or not sites
        or set(ruling.sentence_ids or ()) != sites
        or ruling.rendering != rendering
        or ruling.consistency != decision.consistency
        or decision.required != agreement.required(decision.kind, mode)
    ):
        raise AuthorityError("ruling must match the approved decision and sentence")
    expected_voters = () if decision.required == "SERIAL" else voters
    if tuple(voter for voter, _ in decision.ballots) != expected_voters:
        raise AuthorityError("ballots must come from the authenticated triad")
    if (
        dict(decision.vote_weights) if decision.vote_weights else None
    ) != weights or len(dict(decision.vote_weights)) != len(decision.vote_weights):
        raise AuthorityError(
            "decision weights differ from authenticated runtime weights"
        )
    try:
        approved = approved_rendering(
            decision.examination, decision.required, decision.ballots, weights=weights
        )
    except ValueError as exc:
        raise AuthorityError("invalid authenticated ballot weights") from exc
    if decision.consistency == "SERIAL":
        if approved is not None or decision.approvals != (authority.principal,):
            raise AuthorityError("decision lacks valid principal approval")
        triad: list[str] = []
    else:
        if decision.consistency != decision.required or approved != (
            decision.rendering,
            decision.approvals,
        ):
            raise AuthorityError("decision does not match the verified ballot result")
        triad = list(voters)
    ballots = tuple(
        RulingBallot(
            voter=voter,
            choice=alternatives.get(ballot.choice) if ballot.choice else None,
        )
        for voter, ballot in decision.ballots
    )
    approval = RulingApproval(
        operation=authority.operation, triad=triad, approvers=list(decision.approvals)
    )
    if decision.provisional:
        if base_version != 0:
            raise AuthorityError("a provisional ruling requires a new commitment")
        return ledger.propose(
            actor, identity, ruling, reason, approval=approval, ballots=ballots
        )
    if base_version == 0:
        return ledger.ratify(
            actor,
            identity,
            base_version,
            ruling,
            reason,
            approval=approval,
            ballots=ballots,
        )
    if ledger.snapshot().identities.get(identity.id) != identity:
        raise AuthorityError("revision must refer to the recorded commitment identity")
    return ledger.transition(
        "Revised",
        actor,
        identity.id,
        base_version,
        ruling,
        reason,
        approval=approval,
        ballots=ballots,
    )

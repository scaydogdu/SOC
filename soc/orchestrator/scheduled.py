"""Apply public approval and brief events through the registered principal."""

from soc.episode import Approval, Episode
from soc.ledger.models import (
    Actor,
    Commitment,
    DecisionSubject,
    Quote,
    Rule,
    Ruling,
    RulingApproval,
)
from soc.ledger.store import Ledger


def record_approval(
    ledger: Ledger, principal: Actor, episode: Episode, approval: Approval
) -> None:
    authority = ledger.authority
    if authority is None:
        raise ValueError("scheduled approvals require an operation-scoped ledger")
    identity = Commitment(
        id=approval.decision_id,
        operation=authority.operation,
        owner=principal.agent,
        debtor="executor",
        creditor=principal.agent,
        authority="principal",
        subject=DecisionSubject(
            kind={"term": "term", "non_lexical": "normative", "scope_limited": "term"}[
                approval.kind
            ],
            key=" | ".join(sorted(set(approval.scope_keys))),
        ),
    )
    state = ledger.snapshot()
    old = state.versions.get((identity.id, 1))
    if old is not None:
        if state.identities[identity.id] != identity or old.rendering != approval.value:
            raise ValueError(
                f"ledger approval differs from the episode for {identity.id}"
            )
        return
    scopes = set(approval.scope_keys)
    later = {
        sentence_id
        for chunk in episode.events.chunks[approval.chunk :]
        for sentence_id in chunk.sentence_ids
    }
    applicable = [
        sentence
        for sentence in episode.source
        if sentence.id in later
        and sentence.translatable
        and scopes.intersection(sentence.scope_keys)
    ]
    if not applicable:
        raise ValueError(f"scheduled approval has no later use sites for {identity.id}")
    example = applicable[0]
    ruling = Ruling(
        scope=approval.scope_keys,
        rendering=approval.value,
        source_quote=Quote(sentence_id=example.id, text=example.text),
        rule=Rule(kind=approval.kind, parameters={}),
        rationale="Scheduled principal approval",
        evidence=[
            f"source:{example.id}",
            *(f"brief:{cid}" for cid in approval.brief_clause_ids),
        ],
        proposer=principal.agent,
        ratifier=principal.agent,
        consistency="SERIAL",
        state="ratified",
        sentence_ids=[sentence.id for sentence in applicable],
    )
    event = ledger.ratify(
        principal,
        identity,
        0,
        ruling,
        ruling.rationale,
        approval=RulingApproval(
            operation=authority.operation, approvers=[principal.agent]
        ),
    )
    if event.type != "Ratified":
        raise ValueError(f"scheduled principal ruling conflicted for {identity.id}")


def record_revisions(ledger: Ledger, principal: Actor, episode: Episode) -> None:
    authority = ledger.authority
    if authority is None:
        raise ValueError("scheduled revisions require an operation-scoped ledger")
    change = episode.events.brief_change
    state = ledger.snapshot()
    for revision in change.revisions:
        current = state.current.get(revision.decision_id)
        if current is None:
            raise ValueError(
                f"cannot revise unapproved decision {revision.decision_id}"
            )
        if current.version > 1:
            if current.rendering != revision.value:
                raise ValueError(
                    f"ledger revision differs from the episode for {revision.decision_id}"
                )
            continue
        data = current.model_dump(
            exclude={"commitment_id", "version", "valid_from", "valid_until"}
        )
        data.update(
            rendering=revision.value,
            evidence=[
                *current.evidence,
                *(f"brief:{clause.id}" for clause in change.clauses),
            ],
            rationale="Scheduled principal brief change",
            state="amended",
        )
        ruling = Ruling.model_validate(data)
        event = ledger.transition(
            "Revised",
            principal,
            revision.decision_id,
            current.version,
            ruling,
            ruling.rationale,
            approval=RulingApproval(
                operation=authority.operation, approvers=[principal.agent]
            ),
        )
        if event.type != "Revised":
            raise ValueError(
                f"scheduled revision conflicted for {revision.decision_id}"
            )

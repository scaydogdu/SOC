"""Rebind a durable executor and prepare its prompt-time handover."""

from collections.abc import Callable
from dataclasses import dataclass

from soc.ledger.models import (
    BINDING_STATES,
    Actor,
    CommitmentVersion,
    ExecutorRebound,
    FlagRaised,
)
from soc.ledger.store import Ledger
from soc.memory import ChunkOutput


@dataclass(frozen=True)
class ExecutorHandover:
    ledger_version: int
    commitments: tuple[CommitmentVersion, ...]
    open_flags: dict[str, tuple[str, FlagRaised]]
    partial_output: dict[str, ChunkOutput]


def rebind_executor(
    ledger: Ledger,
    predecessor: Actor,
    successor: Actor,
    *,
    load_output: Callable[[str], ChunkOutput],
) -> ExecutorHandover:
    """Persist or resume the model replacement and read a fresh ledger snapshot."""
    authority = ledger.authority
    if authority is None or predecessor.agent != successor.agent:
        raise ValueError("executor rebind requires the same durable operation agent")
    if predecessor.model == successor.model:
        raise ValueError("successor must use a different model")
    snapshot = ledger.snapshot()
    previous = snapshot.executor
    if previous is None or (previous.old_model, previous.new_model) != (
        predecessor.model,
        successor.model,
    ):
        rebound = ledger.record(
            successor,
            ExecutorRebound(
                old_model=predecessor.model,
                new_model=successor.model,
                ledger_version=snapshot.ledger_version,
            ),
        )
        rebound_sequence = rebound.seq
    else:
        rebound_sequence = previous.ledger_version + 1
    # Use the same partial output and decisions after a crash or restart.
    snapshot = ledger.rebuild(through=rebound_sequence)

    return ExecutorHandover(
        ledger_version=snapshot.ledger_version,
        commitments=tuple(
            version
            for key, version in sorted(snapshot.current.items())
            if snapshot.identities[key].operation == authority.operation
            and (version.state in BINDING_STATES or version.state == "provisional")
        ),
        open_flags=snapshot.open_flags,
        partial_output={
            chunk_id: load_output(build.output_ref)
            for chunk_id, build in snapshot.chunks.items()
            if build.output_ref is not None
        },
    )

"""Use one rebuild path at review, at the gate, and before assembly."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from soc.enforce.checker import Checker, NormativeRendering
from soc.ledger.models import Actor, ChunkBuilt, Event, GateDecision, RepairPath
from soc.ledger.store import Ledger, RepairRequest
from soc.memory import ChunkInput, ChunkOutput
from soc.orchestrator.ledger_checks import gate_chunks

type Repair = Callable[[RepairRequest], Awaitable[ChunkBuilt | None]]


async def repair_on_read(
    ledger: Ledger, actor: Actor, chunk_id: str, path: RepairPath, repair: Repair
) -> Event | None:
    """Attempt one repair if the build is stale. The worker supplies its output reference."""
    request = ledger.repair_request(actor, chunk_id, path)
    if request is None:
        return None
    replacement = await repair(request)
    if replacement is None:
        return None
    if replacement.ledger_version != request.ledger_version:
        raise ValueError("repair must use the requested ledger snapshot")
    return ledger.record(
        actor, replacement, chunk_id=chunk_id, base_build=request.build.build_number
    )


async def gate_with_repair(
    ledger: Ledger,
    actor: Actor,
    chunk_id: str,
    chunk: ChunkInput,
    *,
    repair: Repair,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
) -> Event:
    await repair_on_read(ledger, actor, chunk_id, "gate", repair)
    return gate_chunks(
        ledger,
        actor,
        {chunk_id: (chunk, None)},
        checker=checker,
        load_output=load_output,
        normative=normative,
    )[0]


@dataclass(frozen=True)
class SweepResult:
    decisions: tuple[Event, ...]

    @property
    def accepted(self) -> bool:
        return all(
            GateDecision.model_validate(event.payload["data"]).accepted
            for event in self.decisions
        )


async def final_sweep(
    ledger: Ledger,
    actor: Actor,
    chunks: Mapping[str, ChunkInput],
    *,
    repair: Repair,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
) -> SweepResult:
    expected = dict(zip(ledger.public_chunk_ids, ledger.public_chunks(), strict=True))
    supplied = {chunk_id: chunk.sentences for chunk_id, chunk in chunks.items()}
    positions = {chunk_id: chunk.position for chunk_id, chunk in chunks.items()}
    expected_positions = dict(zip(expected, range(1, len(expected) + 1), strict=True))
    if supplied != expected or positions != expected_positions:
        raise ValueError("the final sweep requires every scheduled public chunk once")
    for chunk_id in chunks:
        await repair_on_read(ledger, actor, chunk_id, "final_sweep", repair)
    return SweepResult(
        gate_chunks(
            ledger,
            actor,
            {key: (chunk, None) for key, chunk in chunks.items()},
            checker=checker,
            load_output=load_output,
            normative=normative,
            path="final_sweep",
        )
    )

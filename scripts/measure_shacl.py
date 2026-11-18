"""Measure validation time for writes at the toy episode's volume."""

from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from soc.ledger.models import (
    Actor,
    BriefChanged,
    ChunkBuilt,
    Commitment,
    ExecutorRebound,
    Quote,
    Rule,
    Ruling,
    VersionRef,
)
from soc.orchestrator.ledger import open_run_ledger

TOY = Path(__file__).resolve().parents[1] / "data" / "episodes" / "toy"


def sample(path: Path) -> list[float]:
    principal = Actor(agent="client", model="human", role="principal")
    executor = Actor(agent="translator", model="model-a", role="executor")
    times: list[float] = []
    with open_run_ledger(TOY, path, "toy") as (episode, ledger):
        schedule = episode.events
        sentences = {sentence.id: sentence for sentence in episode.source}
        for index, chunk in enumerate(schedule.chunks, 1):
            state = ledger.snapshot()
            scope = {
                key for sid in chunk.sentence_ids for key in sentences[sid].scope_keys
            }
            uses = [
                version
                for version in state.current.values()
                if scope.intersection(version.scope)
            ]
            ledger.record(
                executor,
                ChunkBuilt(
                    ledger_version=state.ledger_version,
                    uses=[
                        VersionRef(
                            commitment_id=item.commitment_id, version=item.version
                        )
                        for item in uses
                    ],
                    decisions={item.commitment_id: item.rendering for item in uses},
                    output_ref=f"urn:soc:benchmark:chunk-{index}:1",
                ),
                chunk_id=f"chunk-{index}",
            )
            times.append(ledger.last_validation_ns / 1_000_000)
            for approval in schedule.approvals:
                if approval.chunk != index:
                    continue
                source = next(
                    sentence
                    for sentence in episode.source
                    if set(approval.scope_keys).intersection(sentence.scope_keys)
                )
                identity = Commitment(
                    id=approval.decision_id,
                    operation="toy",
                    owner=principal.agent,
                    debtor=executor.agent,
                    creditor=principal.agent,
                )
                ruling = Ruling(
                    scope=approval.scope_keys,
                    rendering=approval.value,
                    source_quote=Quote(sentence_id=source.id, text=source.text),
                    rule=Rule(kind=approval.kind, parameters={"value": approval.value}),
                    rationale="Approved translation ruling",
                    evidence=[
                        f"source:{source.id}",
                        *(f"brief:{cid}" for cid in approval.brief_clause_ids),
                    ],
                    proposer=executor.agent,
                    ratifier=principal.agent,
                    consistency="ONE",
                    state="ratified",
                )
                ledger.ratify(principal, identity, 0, ruling, "scheduled approval")
                times.append(ledger.last_validation_ns / 1_000_000)
            if index == schedule.executor_change_after:
                ledger.record(
                    executor,
                    ExecutorRebound(
                        old_model=executor.model,
                        new_model="model-b",
                        ledger_version=ledger.snapshot().ledger_version,
                    ),
                )
                times.append(ledger.last_validation_ns / 1_000_000)
                executor = executor.model_copy(update={"model": "model-b"})
            if index == schedule.brief_change.chunk:
                ledger.record(
                    principal,
                    BriefChanged(
                        clauses={
                            clause.id: clause.text
                            for clause in schedule.brief_change.clauses
                        }
                    ),
                )
                times.append(ledger.last_validation_ns / 1_000_000)
                for revision in schedule.brief_change.revisions:
                    current = ledger.snapshot().current[revision.decision_id]
                    data = current.model_dump(
                        exclude={
                            "commitment_id",
                            "version",
                            "valid_from",
                            "valid_until",
                        }
                    )
                    data.update(rendering=revision.value, state="amended")
                    ledger.transition(
                        "Revised",
                        principal,
                        revision.decision_id,
                        current.version,
                        Ruling.model_validate(data),
                        "brief changed",
                    )
                    times.append(ledger.last_validation_ns / 1_000_000)
    return times


def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        warmup = sample(root / "warmup.sqlite")
        values = sorted(
            duration
            for run in range(10)
            for duration in sample(root / f"run-{run}.sqlite")
        )
    p95 = values[ceil(0.95 * len(values)) - 1]
    print(
        f"{len(warmup)} writes per toy run; {len(values)} measured writes after one warm-up"
    )
    print(
        f"SHACL including JSON-LD parsing: median {median(values):.3f} ms, p95 {p95:.3f} ms"
    )


if __name__ == "__main__":
    main()

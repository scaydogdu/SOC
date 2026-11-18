"""Run a public episode against the SOC ledger with a pluggable chunk worker."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from soc.enforce.checker import Checker
from soc.episode import Approval, Brief, Clause, Episode, load_runtime, validate_runtime
from soc.ledger.models import (
    BINDING_STATES,
    Actor,
    ChunkBuilt,
    CommitmentVersion,
    DecisionCaptured,
    DecisionDeclared,
    DecisionUse,
    GateDecision,
    GateFinding,
    OperationAuthority,
)
from soc.ledger.store import ChunkIdBase, Ledger, RevisionHint
from soc.memory import ChunkInput, ChunkOutput
from soc.orchestrator.handover import ExecutorHandover, rebind_executor
from soc.orchestrator.journal import open_journal
from soc.orchestrator.ledger_checks import capture_worker_draft, gate_chunk, gate_chunks
from soc.orchestrator.prompts import digest, json_text
from soc.orchestrator.runner import ExperimentSettings, RunSpec, _write
from soc.orchestrator.scheduled import record_approval, record_revisions
from soc.principal.simulator import PrincipalSimulator
from soc.providers.client import configured_credentials, ensure_no_credentials
from soc.providers.models import Outcome
from soc.triad.agreement import ResolutionMode
from soc.triad.examination import Draft


class LedgerRunSpec(RunSpec):
    resolution: ResolutionMode = "cross_examination"
    max_in_flight: int = Field(default=3, ge=1, le=3)
    chunk_id_base: ChunkIdBase = 1


@dataclass(frozen=True)
class ChunkTask:
    chunk_id: str
    chunk: ChunkInput
    actor: Actor
    ledger_version: int
    brief: Brief
    commitments: dict[str, CommitmentVersion]
    resolution: ResolutionMode
    handover: ExecutorHandover | None
    round_number: int
    previous: ChunkOutput | None
    findings: tuple[GateFinding, ...]
    hints: tuple[RevisionHint, ...]


@dataclass(frozen=True)
class WorkerDraft:
    draft: Draft
    ledger_version: int


type LedgerWorker = Callable[[Ledger, ChunkTask], Awaitable[WorkerDraft | Outcome]]


@dataclass(frozen=True)
class LedgerRunResult:
    status: Literal["completed", "blocked", "budget_exhausted", "failed"]
    outcome: Outcome
    outputs: dict[str, ChunkOutput]


async def run_ledger_episode(
    episode_dir: Path,
    run_dir: Path,
    *,
    spec: LedgerRunSpec,
    experiment: ExperimentSettings,
    principal: PrincipalSimulator,
    checker: Checker,
    worker: LedgerWorker,
    worker_fingerprint: str | None = None,
) -> LedgerRunResult:
    """Drive drafting, durable builds, scheduled events, and final acceptance."""
    episode = validate_runtime(load_runtime(episode_dir))
    fingerprint = digest(
        json_text(
            [
                spec.model_dump(mode="json"),
                experiment.model_dump(mode="json"),
                episode.model_dump(mode="json"),
                principal.actor.model_dump(mode="json"),
                principal.fingerprint,
                *([worker_fingerprint] if worker_fingerprint is not None else []),
            ]
        )
    )
    authority = OperationAuthority(
        operation=spec.episode_id, principal=principal.actor.agent
    )
    with (
        open_journal(run_dir, fingerprint),
        Ledger(
            run_dir / "ledger.sqlite",
            spec.run_id,
            episode=episode,
            authority=authority,
            chunk_id_base=spec.chunk_id_base,
        ) as ledger,
    ):
        run = _LedgerExecution(
            ledger, run_dir, spec, experiment, checker, principal, worker
        )
        return await run.execute(episode)


class _LedgerExecution:
    def __init__(
        self,
        ledger: Ledger,
        directory: Path,
        spec: LedgerRunSpec,
        experiment: ExperimentSettings,
        checker: Checker,
        principal: PrincipalSimulator,
        worker: LedgerWorker,
    ) -> None:
        self.ledger = ledger
        self.directory = directory
        self.spec = spec
        self.experiment = experiment
        self.checker = checker
        self.principal = principal
        self.worker = worker
        self.actor = Actor(agent="executor", model=spec.initial_model, role="executor")
        self.successor = self.actor.model_copy(update={"model": spec.successor_model})
        self.handover: ExecutorHandover | None = None
        self.chunks = {
            key: ChunkInput(position, sentences)
            for position, (key, sentences) in enumerate(
                zip(ledger.public_chunk_ids, ledger.public_chunks(), strict=True), 1
            )
        }

    def load_output(self, ref: str) -> ChunkOutput:
        if len(ref) != 64 or any(char not in "0123456789abcdef" for char in ref):
            raise ValueError("invalid ledger runner output reference")
        data = (self.directory / f"output-{ref}.json").read_text(encoding="utf-8")
        if digest(data) != ref:
            raise ValueError("stored chunk output differs from its ledger reference")
        return ChunkOutput.model_validate_json(data)

    async def build(
        self,
        chunk_id: str,
        findings: tuple[GateFinding, ...] = (),
        *,
        path: Literal["review", "gate", "final_sweep"] = "review",
    ) -> Outcome:
        state = self.ledger.snapshot()
        current = state.chunks.get(chunk_id)
        base = 0 if current is None else current.build_number
        repair = (
            None
            if current is None
            else self.ledger.repair_request(self.actor, chunk_id, path)
        )
        if current is not None and (
            not (findings or repair) or base > self.experiment.max_repair_rounds
        ):
            return Outcome.OK
        chunk = self.chunks[chunk_id]
        clauses = {
            clause.id: clause.text for clause in self.ledger.initial_brief().clauses
        }
        clauses.update(state.brief)
        task = ChunkTask(
            chunk_id=chunk_id,
            chunk=chunk,
            actor=self.actor,
            ledger_version=state.ledger_version,
            brief=Brief(
                clauses=[Clause(id=key, text=text) for key, text in clauses.items()]
            ),
            commitments={
                key: ruling
                for key, ruling in state.current.items()
                if ruling.state in BINDING_STATES or ruling.state == "provisional"
            },
            resolution=self.spec.resolution,
            handover=self.handover,
            round_number=base,
            previous=None
            if current is None or current.output_ref is None
            else self.load_output(current.output_ref),
            findings=findings,
            hints=() if repair is None else repair.hints,
        )
        candidate = await self.worker(self.ledger, task)
        if isinstance(candidate, Outcome):
            if candidate is Outcome.OK:
                raise ValueError("a successful worker must return a draft")
            return candidate
        if candidate.draft.worker != self.actor.agent:
            raise ValueError("chunk worker returned a draft for a different executor")
        translated = [row.sentence_id for row in candidate.draft.output.sentences]
        if len(translated) != len(chunk.sentences) or set(translated) != {
            row.id for row in chunk.sentences
        }:
            raise ValueError("chunk output must contain each scheduled sentence once")
        latest = self.ledger.snapshot()
        if not task.ledger_version <= candidate.ledger_version <= latest.ledger_version:
            raise ValueError("draft must identify the ledger snapshot it used")
        output = candidate.draft.output.model_dump_json()
        ensure_no_credentials(configured_credentials(), output)
        ref = digest(output)
        _write(self.directory, f"output-{ref}.json", output)
        evidence = capture_worker_draft(
            self.ledger,
            self.actor,
            chunk_id,
            chunk,
            candidate.draft,
            checker=self.checker,
            at=candidate.ledger_version,
        )
        observed: dict[tuple[str, int, str], DecisionUse] = {}
        for event in evidence:
            entry: DecisionDeclared | DecisionCaptured
            if event.type == "DecisionDeclared":
                entry = DecisionDeclared.model_validate(event.payload["data"])
                if not entry.verified:
                    continue
            else:
                entry = DecisionCaptured.model_validate(event.payload["data"])
            key = (entry.ref.commitment_id, entry.ref.version)
            version = latest.versions[key]
            if version.valid_from <= candidate.ledger_version and (
                version.valid_until is None
                or candidate.ledger_version < version.valid_until
            ):
                observed[(*key, entry.sentence_id)] = DecisionUse(
                    ref=entry.ref, sentence_id=entry.sentence_id
                )
        sites = list(observed.values())
        uses = list(
            {
                (site.ref.commitment_id, site.ref.version): site.ref for site in sites
            }.values()
        )
        self.ledger.record(
            self.actor,
            ChunkBuilt(
                build_number=base + 1,
                output_ref=ref,
                ledger_version=candidate.ledger_version,
                uses=uses,
                decisions={
                    use.commitment_id: latest.versions[
                        (use.commitment_id, use.version)
                    ].rendering
                    for use in uses
                },
                use_sites=sites,
            ),
            chunk_id=chunk_id,
            base_build=base,
        )
        return Outcome.OK

    async def build_and_gate(
        self,
        chunk_id: str,
        *,
        path: Literal["review", "gate", "final_sweep"] = "review",
    ) -> Outcome:
        findings: tuple[GateFinding, ...] = ()
        while True:
            outcome = await self.build(chunk_id, findings, path=path)
            if outcome is not Outcome.OK:
                return outcome
            current = self.ledger.snapshot().chunks[chunk_id]
            event = gate_chunk(
                self.ledger,
                self.actor,
                chunk_id,
                self.chunks[chunk_id],
                build_number=current.build_number,
                checker=self.checker,
                load_output=self.load_output,
                normative=self.experiment.normative,
            )
            verdict = GateDecision.model_validate(event.payload["data"])
            if (
                verdict.accepted
                or current.build_number > self.experiment.max_repair_rounds
            ):
                return Outcome.OK
            findings = tuple(verdict.findings)
            path = "gate"

    async def execute(self, episode: Episode) -> LedgerRunResult:
        approvals: dict[int, list[Approval]] = defaultdict(list)
        for approval in episode.events.approvals:
            approvals[approval.chunk].append(approval)
        schedule = episode.events
        change = schedule.brief_change
        boundaries = sorted(
            {*approvals, schedule.executor_change_after, change.chunk, len(self.chunks)}
        )
        chunk_ids = list(self.chunks)
        start = 0
        for end in boundaries:
            for offset in range(start, end, self.spec.max_in_flight):
                batch = chunk_ids[offset : min(end, offset + self.spec.max_in_flight)]
                state = self.ledger.snapshot()
                batch = [
                    key
                    for key in batch
                    if key not in state.chunks
                    or key not in state.gates
                    or state.gates[key].build_number != state.chunks[key].build_number
                    or (
                        not state.gates[key].accepted
                        and state.chunks[key].build_number
                        <= self.experiment.max_repair_rounds
                    )
                ]
                async with asyncio.TaskGroup() as group:
                    pending = [
                        group.create_task(self.build_and_gate(key)) for key in batch
                    ]
                for task in pending:
                    if (outcome := task.result()) is not Outcome.OK:
                        return self.publish(outcome, accepted=False)
            start = end
            for approval in approvals[end]:
                record_approval(self.ledger, self.principal.actor, episode, approval)
            if end == schedule.executor_change_after:
                self.handover = rebind_executor(
                    self.ledger,
                    self.actor,
                    self.successor,
                    load_output=self.load_output,
                )
                self.actor = self.successor
            if end == change.chunk:
                self.principal.issue_brief_change(self.ledger, change, after_chunk=end)
                record_revisions(self.ledger, self.principal.actor, episode)
        for chunk_id in sorted(self.ledger.snapshot().stale_chunks):
            outcome = await self.build_and_gate(chunk_id, path="final_sweep")
            if outcome is not Outcome.OK:
                return self.publish(outcome, accepted=False)
        verdicts = gate_chunks(
            self.ledger,
            self.actor,
            {key: (chunk, None) for key, chunk in self.chunks.items()},
            checker=self.checker,
            load_output=self.load_output,
            normative=self.experiment.normative,
            path="final_sweep",
        )
        return self.publish(
            Outcome.OK,
            accepted=all(
                GateDecision.model_validate(event.payload["data"]).accepted
                for event in verdicts
            ),
        )

    def publish(self, outcome: Outcome, *, accepted: bool) -> LedgerRunResult:
        state = self.ledger.snapshot()
        outputs = {
            key: self.load_output(build.output_ref)
            for key, build in state.chunks.items()
            if build.output_ref is not None
        }
        status: Literal["completed", "blocked", "budget_exhausted", "failed"]
        if outcome is Outcome.OK:
            status = "completed" if accepted else "blocked"
        else:
            status = (
                "budget_exhausted" if outcome is Outcome.BUDGET_EXHAUSTED else "failed"
            )
        _write(
            self.directory,
            "final_chunks.jsonl",
            "".join(
                json_text(
                    {
                        "chunk": self.chunks[key].position,
                        **outputs[key].model_dump(mode="json"),
                    }
                )
                + "\n"
                for key in self.chunks
                if key in outputs
            ),
        )
        _write(
            self.directory,
            "final_findings.jsonl",
            "".join(
                json_text(
                    {
                        "chunk": self.chunks[key].position,
                        **state.gates[key].model_dump(mode="json"),
                    }
                )
                + "\n"
                for key in self.chunks
                if key in state.gates
                and state.gates[key].build_number == state.chunks[key].build_number
            ),
        )
        _write(
            self.directory,
            "run.json",
            json_text(
                {
                    **self.spec.model_dump(mode="json"),
                    "status": status,
                    "outcome": outcome,
                    "chunks_completed": len(outputs),
                }
            )
            + "\n",
        )
        return LedgerRunResult(status, outcome, outputs)

"""Sequential translation and a shared repair loop over public episode data."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from soc.enforce.checker import Checker, Finding, NormativeRendering, RuleID
from soc.enforce.lemmatizer import Span
from soc.enforce.meaning import (
    CheckAssessment,
    MeaningResolver,
    MeaningStore,
    MeaningUnavailable,
    ModelMeaningResolver,
    apply_resolution,
    assess,
)
from soc.episode import Brief, Episode, NonBlank, load_runtime, validate_runtime
from soc.interventions import DraftIntervention
from soc.ledger.models import GateFinding, HeldResolution
from soc.memory import ChunkInput, ChunkOutput, DecisionChange, Memory, PromptContext
from soc.orchestrator.journal import Attempt, Journal, open_journal
from soc.orchestrator.prompts import (
    DEFAULT_PROMPTS,
    TranslationPrompts,
    digest,
    json_text,
)
from soc.providers.client import (
    CacheMiss,
    ModelClient,
    configured_credentials,
    ensure_no_credentials,
)
from soc.providers.models import Outcome, ProviderRequestError, Request, Response

type RunStatus = Literal[
    "running", "completed", "budget_exhausted", "failed", "unverified", "blocked"
]


class ExperimentSettings(BaseModel):
    """One repair budget and output policy shared across conditions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_repair_rounds: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    normative: tuple[NormativeRendering, ...] = ()
    require_checkable: bool = True
    meaning_model: str | None = None
    unknown_rate_limit: float = Field(default=0.01, ge=0, le=1, allow_inf_nan=False)


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: NonBlank
    condition: NonBlank
    episode_id: NonBlank
    replicate: int = Field(default=0, ge=0)
    initial_model: NonBlank
    successor_model: NonBlank
    effort: Literal["low", "high"]
    code_commit: NonBlank


class ChunkWorker(Protocol):
    async def __call__(
        self, request: Request[ChunkOutput], *, recover: bool
    ) -> Response[ChunkOutput]: ...


@dataclass(frozen=True)
class RAGChunkTask:
    chunk: ChunkInput
    model: str
    round_number: int
    brief: Brief
    context: PromptContext
    handover: PromptContext | None
    previous: ChunkOutput | None
    findings: tuple[Finding, ...]
    revisions: Sequence[DecisionChange]


class RAGTriadWorker(Protocol):
    fingerprint: str
    model_protocol: dict[str, object]

    async def __call__(self, task: RAGChunkTask, /) -> Attempt: ...

    def remember(self, attempt: Attempt) -> None: ...


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    chunks_completed: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


def _chunks(episode: Episode) -> tuple[ChunkInput, ...]:
    source = {sentence.id: sentence for sentence in episode.source}
    return tuple(
        ChunkInput(position, tuple(source[key] for key in chunk.sentence_ids))
        for position, chunk in enumerate(episode.events.chunks, 1)
    )


def _write(directory: Path, name: str, content: str) -> None:
    temporary = directory / f".{name}.tmp"
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(directory / name)


def _lines(items: Sequence[object]) -> str:
    return "".join(json_text(item) + "\n" for item in items)


async def run_episode(
    episode_dir: Path,
    run_dir: Path,
    *,
    spec: RunSpec,
    experiment: ExperimentSettings,
    memory: Memory,
    checker: Checker,
    client: ModelClient,
    worker: ChunkWorker | None = None,
    prompts_dir: Path = DEFAULT_PROMPTS,
    triad_worker: RAGTriadWorker | None = None,
    meaning_resolver: MeaningResolver | None = None,
    draft_intervention: DraftIntervention | None = None,
) -> RunResult:
    """Run or resume using a fresh memory instance and model client for this run."""
    if (
        client.run_id != spec.run_id
        or client.tokens
        or client.run_dir != run_dir.resolve()
    ):
        raise ValueError("runner requires a fresh model client for this run ID")
    episode = validate_runtime(
        load_runtime(episode_dir),
        require_checkable=experiment.require_checkable,
        normative=experiment.normative,
    )
    if meaning_resolver is None and experiment.meaning_model is not None:
        meaning_resolver = ModelMeaningResolver(
            client,
            experiment.meaning_model,
            spec.effort,
            spec.replicate,
            experiment.max_output_tokens,
        )
    chunks = _chunks(episode)
    prompts = TranslationPrompts(prompts_dir)
    manifest = {
        **spec.model_dump(mode="json"),
        "episode_sha256": digest(episode.model_dump_json()),
        "experiment": experiment.model_dump(mode="json"),
        "meaning_resolver": None
        if meaning_resolver is None
        else meaning_resolver.actor.model_dump(mode="json"),
        "template_hashes": prompts.hashes(),
        "model_client_limits": client.limits.model_dump(mode="json"),
        "model_prices": {
            key: price.model_dump() for key, price in sorted(client.prices.items())
        },
    }
    if triad_worker is not None:
        if draft_intervention is not None:
            raise ValueError("configure draft intervention on the triad worker only")
        if worker is not None:
            raise ValueError("configure one worker protocol per RAG run")
        manifest["triad_worker"] = triad_worker.fingerprint
        manifest["model_protocol"] = triad_worker.model_protocol
    if draft_intervention is not None:
        manifest["draft_intervention"] = draft_intervention.fingerprint
    manifest_json = json_text(manifest)
    ensure_no_credentials(
        configured_credentials(), manifest_json, episode.model_dump_json()
    )
    resuming = (run_dir / "journal.sqlite").is_file()
    with open_journal(run_dir, digest(manifest_json)) as journal:
        usage_restored = resuming and (
            triad_worker is not None
            or isinstance(meaning_resolver, ModelMeaningResolver)
        )
        if usage_restored:
            client.restore_recorded_usage()
        execution = _Execution(
            spec,
            experiment,
            memory,
            checker,
            client,
            worker or client.generate,
            prompts,
            journal,
            triad_worker,
            MeaningStore(run_dir / "meaning.sqlite"),
            meaning_resolver,
            usage_restored,
            draft_intervention,
        )
        if not (run_dir / "run.json").exists():
            execution.publish(run_dir, manifest, "running", chunks)
        try:
            outcome = await execution.run(episode, chunks)
        except MeaningUnavailable as error:
            return execution.publish(
                run_dir,
                manifest,
                "budget_exhausted"
                if error.outcome is Outcome.BUDGET_EXHAUSTED
                else "failed",
                chunks,
                error.outcome,
            )
        except (CacheMiss, ProviderRequestError) as error:
            failure = (
                Outcome.CACHE_MISS
                if isinstance(error, CacheMiss)
                else Outcome.PARSE_FAILED
            )
            execution.publish(run_dir, manifest, "failed", chunks, failure)
            raise
        status: RunStatus = (
            "completed"
            if outcome is Outcome.OK
            else "unverified"
            if outcome is Outcome.UNVERIFIED
            else "blocked"
            if outcome is Outcome.HELD_VIOLATION
            else (
                "budget_exhausted" if outcome is Outcome.BUDGET_EXHAUSTED else "failed"
            )
        )
        return execution.publish(run_dir, manifest, status, chunks, outcome)


class _Execution:
    def __init__(
        self,
        spec: RunSpec,
        experiment: ExperimentSettings,
        memory: Memory,
        checker: Checker,
        client: ModelClient,
        worker: ChunkWorker,
        prompts: TranslationPrompts,
        journal: Journal,
        triad_worker: RAGTriadWorker | None,
        meaning: MeaningStore,
        meaning_resolver: MeaningResolver | None,
        usage_restored: bool,
        draft_intervention: DraftIntervention | None,
    ) -> None:
        self.spec = spec
        self.experiment = experiment
        self.memory = memory
        self.checker = checker
        self.client = client
        self.worker = worker
        self.prompts = prompts
        self.journal = journal
        self.triad_worker = triad_worker
        self.model = spec.initial_model
        self.brief = Brief(clauses=[])
        self.handover: PromptContext | None = None
        self.outputs: dict[int, ChunkOutput] = {}
        self.rounds: dict[int, int] = {}
        self.delivered: list[dict[str, object]] = []
        self.violations: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.attempts: list[Attempt] = []
        self.meaning, self.meaning_resolver = meaning, meaning_resolver
        self.usage_restored = usage_restored
        self.draft_intervention = draft_intervention
        # Late resolutions must not affect earlier drafting when replaying the schedule.
        self.resolutions: dict[str, HeldResolution] = {}
        self.resolution_events: set[str] = set()

    def _check(self, chunk: ChunkInput, output: ChunkOutput) -> tuple[Finding, ...]:
        findings, assessment = self._assessment(chunk, output)
        self.meaning.observe(
            assessment.context_id, assessment.must_checks, assessment.held
        )
        checks = {row.check_id: row for row in assessment.held}
        effective = [
            apply_resolution(
                row, checks[row.check_id], self.resolutions.get(row.check_id)
            )
            if row.check_id is not None and row.check_id in checks
            else row
            for row in findings
        ]
        return tuple(
            Finding(
                cast(RuleID, row.rule),
                row.sentence_id or "",
                row.detail,
                span=None
                if row.start is None or row.end is None
                else Span(row.start, row.end),
                decision_id=row.commitment_id,
                brief_clause_ids=tuple(row.brief_clause_ids),
                strength=row.strength or "must",
                status=(
                    "unknown"
                    if row.outcome in {"unknown", "held"}
                    else "compliant"
                    if row.check_id is not None and row.outcome == "allowed"
                    else "violation"
                ),
                unknown_cause=row.unknown_cause,
            )
            for row in effective
        )

    def _assessment(
        self, chunk: ChunkInput, output: ChunkOutput
    ) -> tuple[list[GateFinding], CheckAssessment]:
        return assess(
            self.checker,
            f"chunk-{chunk.position}",
            self.rounds.get(chunk.position, 0) + 1,
            chunk.sentences,
            output,
            self.brief,
            decisions=self.memory.decisions_for(chunk),
            forbidden=self.memory.forbidden_for(chunk),
            normative=self.experiment.normative,
        )

    async def _attempt(
        self, chunk: ChunkInput, revisions: Sequence[DecisionChange]
    ) -> tuple[Outcome, tuple[Finding, ...]]:
        position = chunk.position
        round_number = self.rounds.get(position, -1) + 1
        call_id = f"chunk-{position}-round-{round_number}"
        previous = self.outputs.get(position)
        context = self.memory.context_for(chunk, self.brief)
        handover = self.handover
        prior_findings = self._check(chunk, previous) if previous is not None else ()
        messages = self.prompts.render(
            chunk,
            context,
            handover=handover,
            previous=previous,
            findings=prior_findings,
            revisions=revisions,
        )
        prompt_hash = digest(json_text([message.model_dump() for message in messages]))
        request = Request(
            model=self.model,
            effort=self.spec.effort,
            messages=messages,
            schema=ChunkOutput,
            replicate=self.spec.replicate,
            run_id=self.spec.run_id,
            call_id=call_id,
            sampling={"max_output_tokens": self.experiment.max_output_tokens},
        )
        recovering, recorded = self.journal.prepare(call_id, prompt_hash)
        if recorded is None and self.triad_worker is not None:
            recorded = await self.triad_worker(
                RAGChunkTask(
                    chunk,
                    self.model,
                    round_number,
                    self.brief,
                    context,
                    handover,
                    previous,
                    prior_findings,
                    revisions,
                )
            )
            if recorded.output is not None:
                ensure_no_credentials(
                    configured_credentials(), recorded.output.model_dump_json()
                )
            self.journal.complete(call_id, recorded)
        elif recorded is None:
            response = await self.worker(request, recover=recovering)
            translated = response.parsed
            if translated is not None and self.draft_intervention is not None:
                translated = self.draft_intervention.apply(
                    position, round_number, "executor", translated
                )
            if translated is not None:
                ensure_no_credentials(
                    configured_credentials(), translated.model_dump_json()
                )
            recorded = Attempt(
                outcome=response.reply.outcome,
                output=translated,
                input_tokens=response.reply.input_tokens,
                output_tokens=response.reply.output_tokens,
                estimated_cost_usd=response.estimated_cost_usd,
            )
            self.journal.complete(call_id, recorded)
        elif self.triad_worker is None and not self.usage_restored:
            self.client.tokens += recorded.input_tokens + recorded.output_tokens
        if self.triad_worker is not None:
            self.triad_worker.remember(recorded)
            self.events.extend(
                {
                    "event": "principal_answered",
                    "chunk": position,
                    **answer.model_dump(mode="json"),
                }
                for answer in recorded.principal_answers
            )
            self.events.extend(
                {
                    "event": "flag_resolved",
                    "chunk": position,
                    "round": round_number,
                    "trace": trace.model_dump(mode="json"),
                }
                for trace in recorded.resolutions
            )
        self.attempts.append(recorded)
        self.rounds[position] = round_number
        if recorded.outcome is Outcome.BUDGET_EXHAUSTED and not (
            recorded.input_tokens or recorded.output_tokens
        ):
            return recorded.outcome, ()

        self.handover = None
        references = context.delivered + (
            handover.delivered if handover is not None else ()
        )
        delivered = PromptContext(context.text, tuple(dict.fromkeys(references)))
        self.delivered.append(
            {
                "call_id": call_id,
                "chunk": position,
                "round": round_number,
                "model": self.model,
                "prompt_hash": prompt_hash,
                "decisions": [asdict(ref) for ref in delivered.delivered],
            }
        )
        findings: tuple[Finding, ...] = ()
        if recorded.output is not None:
            findings = self._check(chunk, recorded.output)
            self.outputs[position] = recorded.output
            self.memory.after_output(chunk, recorded.output, delivered)
        self.violations.append(
            {
                "call_id": call_id,
                "chunk": position,
                "round": round_number,
                "outcome": recorded.outcome,
                "findings": [asdict(finding) for finding in findings],
            }
        )
        return recorded.outcome, findings

    async def _build(
        self, chunk: ChunkInput, revisions: Sequence[DecisionChange] = ()
    ) -> Outcome:
        if self.rounds.get(chunk.position, -1) >= self.experiment.max_repair_rounds:
            return Outcome.OK
        outcome, findings = await self._attempt(chunk, revisions)
        while (
            outcome is Outcome.OK
            and self.rounds[chunk.position] < self.experiment.max_repair_rounds
            and any(finding.status == "violation" for finding in findings)
        ):
            outcome, findings = await self._attempt(chunk, revisions)
        return outcome

    def _change_brief(self, episode: Episode) -> dict[int, list[DecisionChange]]:
        change = episode.events.brief_change
        clauses = {clause.id: clause for clause in self.brief.clauses}
        clauses.update({clause.id: clause for clause in change.clauses})
        self.brief = self.brief.model_copy(update={"clauses": list(clauses.values())})
        self.events.append(
            {
                "event": "brief_changed",
                "after_chunk": change.chunk,
                "clause_ids": [clause.id for clause in change.clauses],
            }
        )
        revisions = [
            self.memory.apply_revision(revision, self.brief)
            for revision in change.revisions
        ]
        repairs: dict[int, list[DecisionChange]] = defaultdict(list)
        for revision in revisions:
            affected = sorted(set(self.memory.affected_chunks(revision)))
            self.events.append(
                {
                    "event": "revised",
                    "after_chunk": change.chunk,
                    "decision": asdict(revision.current.ref),
                    "affected_chunks": affected,
                }
            )
            for position in affected:
                if position not in self.outputs:
                    raise ValueError(
                        "memory returned an affected chunk without saved output"
                    )
                repairs[position].append(revision)
        return repairs

    async def run(self, episode: Episode, chunks: tuple[ChunkInput, ...]) -> Outcome:
        self.brief = episode.brief
        approvals = defaultdict(list)
        for approval in episode.events.approvals:
            approvals[approval.chunk].append(approval)
        for chunk in chunks:
            outcome = await self._build(chunk)
            if outcome is not Outcome.OK:
                return outcome
            for approval in approvals[chunk.position]:
                decision = self.memory.record_approval(approval, self.brief)
                self.events.append(
                    {
                        "event": "approved",
                        "after_chunk": chunk.position,
                        "decision": asdict(decision.ref),
                    }
                )
            repairs = (
                self._change_brief(episode)
                if chunk.position == episode.events.brief_change.chunk
                else {}
            )
            if chunk.position == episode.events.executor_change_after:
                self.model = self.spec.successor_model
                self.handover = self.memory.executor_change(self.brief)
                self.events.append(
                    {
                        "event": "executor_changed",
                        "after_chunk": chunk.position,
                        "model": self.model,
                    }
                )
            for position, revisions in sorted(repairs.items()):
                outcome = await self._build(chunks[position - 1], revisions)
                if outcome is not Outcome.OK:
                    return outcome
        return await self._resolve_held(chunks)

    async def _resolve_held(self, chunks: tuple[ChunkInput, ...]) -> Outcome:
        for _ in range(self.experiment.max_repair_rounds + 2):
            repaired = False
            for position, output in list(sorted(self.outputs.items())):
                chunk = chunks[position - 1]
                _, assessment = self._assessment(chunk, output)
                self.meaning.observe(
                    assessment.context_id, assessment.must_checks, assessment.held
                )
                violation = False
                for check in assessment.held:
                    result = await self.meaning.resolve(check, self.meaning_resolver)
                    if result is not None:
                        self.resolutions[result.check_id] = result
                        violation |= result.verdict == "violation"
                        if result.check_id not in self.resolution_events:
                            self.events.append(
                                {
                                    "event": "HeldResolved",
                                    **result.model_dump(mode="json"),
                                }
                            )
                            self.resolution_events.add(result.check_id)
                if violation:
                    before = self.rounds[position]
                    outcome = await self._build(chunk)
                    if outcome is not Outcome.OK:
                        return outcome
                    repaired |= self.rounds[position] > before
            if not repaired:
                break
        final = [
            finding
            for position, output in self.outputs.items()
            for finding in self._check(chunks[position - 1], output)
        ]
        if any(
            row.status == "violation" and row.unknown_cause is not None for row in final
        ):
            return Outcome.HELD_VIOLATION
        if any(row.status == "unknown" and row.strength == "must" for row in final):
            return Outcome.UNVERIFIED
        return Outcome.OK

    def publish(
        self,
        directory: Path,
        manifest: dict[str, object],
        status: RunStatus,
        chunks: tuple[ChunkInput, ...],
        outcome: Outcome | None = None,
    ) -> RunResult:
        meaning_usage = self.meaning.usage(self.meaning_resolver)
        result = RunResult(
            status,
            len(self.outputs),
            sum(attempt.input_tokens for attempt in self.attempts)
            + meaning_usage.input_tokens,
            sum(attempt.output_tokens for attempt in self.attempts)
            + meaning_usage.output_tokens,
            sum(attempt.estimated_cost_usd for attempt in self.attempts)
            + meaning_usage.estimated_cost_usd,
        )
        final = [
            {
                "chunk": position,
                "findings": [
                    asdict(finding)
                    | (
                        {"status": "unverified"}
                        if finding.status == "unknown" and finding.strength == "must"
                        else {}
                    )
                    for finding in self._check(chunks[position - 1], output)
                ],
            }
            for position, output in sorted(self.outputs.items())
        ]
        reports = {
            "final_chunks.jsonl": [
                {"chunk": position, **output.model_dump(mode="json")}
                for position, output in sorted(self.outputs.items())
            ],
            "delivered_decisions.jsonl": self.delivered,
            "violations.jsonl": self.violations,
            "final_findings.jsonl": final,
            "events.jsonl": self.events,
        }
        _write(
            directory,
            "unknowns.json",
            json_text(
                {
                    "condition": self.spec.condition,
                    "episode_id": self.spec.episode_id,
                    "run_id": self.spec.run_id,
                    **self.meaning.report(
                        limit=self.experiment.unknown_rate_limit,
                        unverified_final=sum(
                            finding.status == "unknown" and finding.strength == "must"
                            for position, output in self.outputs.items()
                            for finding in self._check(chunks[position - 1], output)
                        ),
                    ),
                }
            )
            + "\n",
        )
        _write(
            directory,
            "assembly.json",
            json_text(
                {
                    "accepted": status == "completed",
                    "status": status,
                }
            )
            + "\n",
        )
        for name, rows in reports.items():
            _write(directory, name, _lines(rows))
        _write(
            directory,
            "run.json",
            json_text(
                {
                    **manifest,
                    **asdict(result),
                    "meaning_usage": asdict(meaning_usage),
                    "outcome": outcome,
                    "tokens": result.input_tokens + result.output_tokens,
                    "prompt_hashes": {
                        str(item["call_id"]): item["prompt_hash"]
                        for item in self.delivered
                    },
                }
            )
            + "\n",
        )
        return result

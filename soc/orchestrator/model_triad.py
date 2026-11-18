"""Drive the existing triad protocol through the shared structured model client."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

from soc.enforce.checker import Checker
from soc.episode import Brief, Clause
from soc.ledger.models import (
    BINDING_STATES,
    Actor,
    Commitment,
    CommitmentVersion,
    DecisionSubject,
    DecisionUse,
    FlagRaised,
    FlagResolved,
    Quote,
    Rule,
    Ruling,
    Text,
    VersionRef,
)
from soc.ledger.store import Ledger
from soc.memory import ChunkOutput
from soc.orchestrator.ledger import record_triad_ruling
from soc.orchestrator.ledger_checks import capture_worker_draft
from soc.orchestrator.ledger_runner import (
    ChunkTask,
    LedgerRunResult,
    LedgerRunSpec,
    WorkerDraft,
    run_ledger_episode,
)
from soc.orchestrator.prompts import digest, json_text
from soc.orchestrator.runner import ExperimentSettings, _write
from soc.principal.simulator import PrincipalSimulator, UnansweredQuestion
from soc.providers.client import (
    CacheMiss,
    ModelClient,
    configured_credentials,
    ensure_no_credentials,
)
from soc.providers.models import Message, Mode, Outcome, ProviderRequestError, Request
from soc.triad.agreement import (
    AgreementRules,
    Ballot,
    Classification,
    Decision,
    DecisionType,
    PrincipalResolution,
    run_triad,
)
from soc.triad.examination import (
    Answer,
    Challenge,
    Difference,
    Draft,
    Examination,
    Turn,
    align_drafts,
)


@dataclass(frozen=True)
class ModelTeam:
    peer_model: str
    reviewer_model: str

    def validate(self, spec: LedgerRunSpec, client: ModelClient) -> None:
        models = {spec.initial_model, self.peer_model, self.reviewer_model}
        if len(models) != 3 or spec.successor_model in models:
            raise ValueError(
                "executor, successor, peer and reviewer need distinct model IDs"
            )
        if not (models | {spec.successor_model}) <= client.prices.keys():
            raise ValueError("each triad model needs a configured price")
        if client.mode is not Mode.REPLAY:
            families = {client.route_family(model) for model in models}
            if None in families or len(families) != 3:
                raise ValueError("a triad needs routes from three model families")
            if client.route_family(spec.initial_model) != client.route_family(
                spec.successor_model
            ):
                raise ValueError("the successor must use the executor's model family")


class DraftReply(ChunkOutput):
    manifest: list[DecisionUse]


class ReviewReply(BaseModel, extra="forbid"):
    challenge: Challenge | None


class ResponseReply(BaseModel, extra="forbid"):
    answer: Answer | None


class DiscussionReply(BaseModel, extra="forbid"):
    remark: Text | None


class _PreparedCall(BaseModel, extra="forbid"):
    call_id: str
    model: str
    schema_name: str
    messages: tuple[Message, ...]


class _CallFailed(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


def _examination(examination: Examination) -> dict[str, object]:
    return {
        "difference": asdict(examination.difference),
        "turns": [
            {
                "challenge": turn.challenge.model_dump(mode="json"),
                "answer": None
                if turn.answer is None
                else turn.answer.model_dump(mode="json"),
            }
            for turn in examination.turns
        ],
        "discussion": [asdict(remark) for remark in examination.discussion],
    }


async def run_model_ledger_episode(
    episode_dir: Path,
    run_dir: Path,
    *,
    spec: LedgerRunSpec,
    experiment: ExperimentSettings,
    principal: PrincipalSimulator,
    checker: Checker,
    client: ModelClient,
    team: ModelTeam,
    agreement: AgreementRules,
) -> LedgerRunResult:
    """Run the model-backed SOC condition with recording, replay, or fake routes."""
    if client.run_id != spec.run_id or client.run_dir != run_dir.resolve():
        raise ValueError("model client must belong to the SOC run")
    team.validate(spec, client)
    worker = _ModelTriad(spec, experiment, client, team, agreement, principal, checker)
    fingerprint = digest(
        json_text(
            [
                asdict(team),
                agreement.model_dump(mode="json"),
                client.limits.model_dump(mode="json"),
                {
                    model: price.model_dump()
                    for model, price in sorted(client.prices.items())
                },
            ]
        )
    )
    if (run_dir / "ledger.sqlite").exists():
        client.restore_recorded_usage()
    return await run_ledger_episode(
        episode_dir,
        run_dir,
        spec=spec,
        experiment=experiment,
        principal=principal,
        checker=checker,
        worker=worker,
        worker_fingerprint=fingerprint,
    )


class _ModelTriad:
    def __init__(
        self,
        spec: LedgerRunSpec,
        experiment: ExperimentSettings,
        client: ModelClient,
        team: ModelTeam,
        agreement: AgreementRules,
        principal: PrincipalSimulator,
        checker: Checker,
    ) -> None:
        self.spec = spec
        self.experiment = experiment
        self.client = client
        self.agreement = agreement
        self.principal = principal
        self.checker = checker
        self.peer = Actor(agent="peer", model=team.peer_model, role="worker")
        self.reviewer = Actor(
            agent="reviewer", model=team.reviewer_model, role="reviewer"
        )

    async def __call__(self, ledger: Ledger, task: ChunkTask) -> WorkerDraft | Outcome:
        try:
            return await self._run(ledger, task)
        except _CallFailed as error:
            return error.outcome
        except CacheMiss:
            return Outcome.CACHE_MISS
        except (ProviderRequestError, UnansweredQuestion):
            return Outcome.PARSE_FAILED

    async def _run(self, ledger: Ledger, task: ChunkTask) -> WorkerDraft:
        actors = {
            actor.agent: actor for actor in (task.actor, self.peer, self.reviewer)
        }
        source = task.chunk.sentences
        by_id = {sentence.id: sentence for sentence in source}
        classified: dict[str, Classification] = {}
        used_versions = [
            VersionRef(commitment_id=row.commitment_id, version=row.version)
            for row in task.commitments.values()
        ]

        def read_evidence() -> tuple[Brief, Mapping[str, CommitmentVersion]]:
            state = ledger.snapshot()
            clauses = {clause.id: clause.text for clause in task.brief.clauses}
            clauses.update(state.brief)
            return Brief(
                clauses=[Clause(id=key, text=text) for key, text in clauses.items()]
            ), state.current

        async def ask[T: BaseModel](
            actor: Actor,
            step: str,
            instruction: str,
            schema: type[T],
            payload: object,
        ) -> T:
            call_id = f"{task.chunk_id}/r{task.round_number}/{step}"
            messages = (
                Message(role="system", content=instruction),
                Message(role="user", content=json_text(payload)),
            )
            path = self.client.run_dir / f"triad-prompt-{digest(call_id)}.json"
            prepared = _PreparedCall(
                call_id=call_id,
                model=actor.model,
                schema_name=schema.__name__,
                messages=messages,
            )
            if path.exists():
                stored = _PreparedCall.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if (stored.call_id, stored.model, stored.schema_name) != (
                    prepared.call_id,
                    prepared.model,
                    prepared.schema_name,
                ):
                    raise ValueError(f"prepared triad call changed for {call_id}")
                prepared = stored
            else:
                raw = prepared.model_dump_json()
                ensure_no_credentials(configured_credentials(), raw)
                _write(self.client.run_dir, path.name, raw)
            reply = await self.client.generate(
                Request(
                    model=actor.model,
                    effort=self.spec.effort,
                    messages=prepared.messages,
                    schema=schema,
                    replicate=self.spec.replicate,
                    run_id=self.spec.run_id,
                    call_id=call_id,
                    sampling={"max_output_tokens": self.experiment.max_output_tokens},
                ),
                recover=True,
            )
            if reply.parsed is None:
                raise _CallFailed(reply.reply.outcome)
            return reply.parsed

        def relevant(
            commitments: Mapping[str, CommitmentVersion],
        ) -> list[dict[str, object]]:
            return [
                {
                    "ref": {
                        "commitment_id": version.commitment_id,
                        "version": version.version,
                    },
                    "rendering": version.rendering,
                    "scope_keys": version.scope,
                    "sentence_ids": version.sentence_ids,
                    "state": version.state,
                    "strength": version.strength,
                }
                for _, version in sorted(commitments.items())
                if (version.state in BINDING_STATES or version.state == "provisional")
                and task.chunk.scope_keys.intersection(version.scope)
                and (
                    version.sentence_ids is None
                    or by_id.keys() & set(version.sentence_ids)
                )
            ]

        def working_context(sentence_id: str) -> dict[str, object]:
            brief, commitments = read_evidence()
            return {
                "source": by_id[sentence_id].model_dump(mode="json"),
                "brief": brief.model_dump(mode="json"),
                "commitments": relevant(commitments),
                "revision_hints": [
                    {
                        "used": hint.used.model_dump(mode="json"),
                        "current_rendering": hint.current.rendering,
                    }
                    for hint in ledger.revision_hints(used_versions)
                    if hint.current.sentence_ids is None
                    or sentence_id in hint.current.sentence_ids
                ],
            }

        async def draft(actor: Actor) -> Draft:
            handover = task.handover if actor == task.actor else None
            reply = await ask(
                actor,
                f"draft/{actor.agent}",
                "Translate the source from English to Turkish. Follow the active brief and applicable "
                "commitments. Preserve normative force, numbers, and non-translatable source exactly. "
                "Return one translation per sentence ID. Declare each commitment version you used.",
                DraftReply,
                {
                    "source": [sentence.model_dump(mode="json") for sentence in source],
                    "brief": task.brief.model_dump(mode="json"),
                    "commitments": relevant(task.commitments),
                    "previous": None
                    if task.previous is None
                    else task.previous.model_dump(mode="json"),
                    "gate_findings": [
                        row.model_dump(mode="json") for row in task.findings
                    ],
                    "repair_hints": [
                        hint.current.model_dump(mode="json") for hint in task.hints
                    ],
                    "handover": None
                    if handover is None
                    else {
                        "ledger_version": handover.ledger_version,
                        "commitments": [
                            row.model_dump(mode="json") for row in handover.commitments
                        ],
                        "open_flags": {
                            key: (chunk_id, flag.model_dump(mode="json"))
                            for key, (chunk_id, flag) in sorted(
                                handover.open_flags.items()
                            )
                        },
                        "partial_output": {
                            key: output.model_dump(mode="json")
                            for key, output in sorted(handover.partial_output.items())
                        },
                    },
                },
            )
            return Draft(
                actor.agent,
                ChunkOutput(sentences=reply.sentences),
                tuple(reply.manifest),
            )

        first = await draft(task.actor)
        second = await draft(self.peer)
        try:
            differences = align_drafts(task.chunk, first, second)
        except ValueError as exc:
            raise _CallFailed(Outcome.PARSE_FAILED) from exc
        for difference in differences:
            ledger.record(
                self.reviewer,
                FlagRaised(
                    flag_id=f"{task.chunk_id}/r{task.round_number}/{difference.sentence_id}",
                    sentence_id=difference.sentence_id,
                    reason="Draft renderings differ",
                ),
                chunk_id=task.chunk_id,
            )

        async def review(
            difference: Difference, history: tuple[Turn, ...]
        ) -> Challenge | None:
            sid = difference.sentence_id
            reply = await ask(
                self.reviewer,
                f"{sid}/review/{len(history) + 1}",
                "Ask one evidence question to the worker whose rendering is challenged. "
                "Return a null challenge when no further question is needed.",
                ReviewReply,
                working_context(sid)
                | _examination(Examination(difference, history))
                | {"draft_workers": [first.worker, second.worker]},
            )
            if reply.challenge is not None and reply.challenge.worker not in {
                first.worker,
                second.worker,
            }:
                raise _CallFailed(Outcome.PARSE_FAILED)
            return reply.challenge

        response_rounds: dict[str, int] = {}

        async def respond(
            difference: Difference, challenge: Challenge
        ) -> Answer | None:
            sid = difference.sentence_id
            round_number = response_rounds.get(sid, 0) + 1
            response_rounds[sid] = round_number
            reply = await ask(
                actors[challenge.worker],
                f"{sid}/response/{round_number}",
                "Answer the challenge briefly and quote the source, a ratified commitment, or the brief. "
                "Cite exact references as source:<sentence_id>, brief:<id>, or commitment:<id>.",
                ResponseReply,
                working_context(sid)
                | {
                    "difference": asdict(difference),
                    "challenge": challenge.model_dump(mode="json"),
                    "your_draft": "first"
                    if challenge.worker == first.worker
                    else "second",
                },
            )
            return reply.answer

        async def discuss(agent: str, examination: Examination) -> str | None:
            sid = examination.difference.sentence_id
            reply = await ask(
                actors[agent],
                f"{sid}/discussion/{agent}",
                "Comment briefly on the competing drafts, considering earlier remarks.",
                DiscussionReply,
                working_context(sid) | _examination(examination),
            )
            return reply.remark

        async def classify(examination: Examination) -> Classification:
            sid = examination.difference.sentence_id
            reply = await ask(
                self.reviewer,
                f"{sid}/classify",
                "Classify the difference. Use normative for force, polarity or numbers and brief_gap "
                "when a preference is missing. For a reusable term give the exact source scope key "
                "and the distinct verbatim term in each draft. Otherwise set term to null. "
                "For a gap give one short question for the principal.",
                Classification,
                working_context(sid) | _examination(examination),
            )
            if reply.term is not None:
                try:
                    reply.term.validate_difference(examination)
                    ledger.reusable_term_sites(sid, reply.term.scope_key)
                except ValueError as exc:
                    raise _CallFailed(Outcome.PARSE_FAILED) from exc
            classified[sid] = reply
            return reply

        async def vote(
            agent: str, examination: Examination, kind: DecisionType
        ) -> Ballot:
            sid = examination.difference.sentence_id
            return await ask(
                actors[agent],
                f"{sid}/vote/{agent}",
                "Vote for the first or second rendering using the source, brief and recorded evidence. "
                "Abstain with null when neither rendering is supported.",
                Ballot,
                working_context(sid)
                | _examination(examination)
                | {"decision_kind": kind},
            )

        async def settle(
            _agent: str, examination: Examination, kind: DecisionType
        ) -> PrincipalResolution:
            sid = examination.difference.sentence_id
            classification = classified[sid]
            if classification.term is not None:
                question = f"Which Turkish rendering should we use for {classification.term.scope_key}?"
            else:
                question = (
                    classification.question
                    or f"What is the approved {kind} rendering for {sid}?"
                )
            answer = await self.principal.answer(
                ledger,
                f"{task.chunk_id}/r{task.round_number}/{sid}",
                question,
                chunk_id=task.chunk_id,
            )
            return PrincipalResolution(rendering=answer)

        result = await run_triad(
            task.chunk,
            first,
            second,
            source=source,
            brief=task.brief,
            commitments=task.commitments,
            reviewer=self.reviewer.agent,
            principal=self.principal.actor.agent,
            agreement=self.agreement,
            classify=classify,
            vote=vote,
            settle=settle,
            review=review,
            respond=respond,
            mode=task.resolution,
            discuss=discuss,
            read_evidence=read_evidence,
        )
        for original in (first, second):
            capture_worker_draft(
                ledger,
                actors[original.worker],
                task.chunk_id,
                task.chunk,
                original,
                checker=self.checker,
                at=task.ledger_version,
            )
        observed = task.ledger_version
        for decision in result.decisions:
            sid = decision.examination.difference.sentence_id
            if decision.term is not None:
                updated = self._record_term(ledger, task, decision)
                if updated is not None:
                    observed = max(observed, updated)
            ledger.record(
                self.reviewer,
                FlagResolved(flag_id=f"{task.chunk_id}/r{task.round_number}/{sid}"),
                chunk_id=task.chunk_id,
            )
        return WorkerDraft(Draft(task.actor.agent, result.draft), observed)

    def _record_term(
        self, ledger: Ledger, task: ChunkTask, decision: Decision
    ) -> int | None:
        assert decision.term is not None and decision.term_rendering is not None
        key = decision.term.scope_key
        sid = decision.examination.difference.sentence_id
        sites = ledger.reusable_term_sites(sid, key)
        if not sites:
            return None
        actor = (
            self.principal.actor if decision.consistency == "SERIAL" else self.reviewer
        )
        authority = ledger.authority
        assert authority is not None
        identifier = f"term-{digest(json_text([key, sid]))[:24]}"
        state = ledger.snapshot()
        if current := state.current.get(identifier):
            return max(task.ledger_version, current.valid_from)
        sentence = next(row for row in task.chunk.sentences if row.id == sid)
        identity = Commitment(
            id=identifier,
            operation=authority.operation,
            owner=actor.agent,
            debtor=task.actor.agent,
            creditor=self.principal.actor.agent,
            subject=DecisionSubject(kind="term", key=key),
            authority="principal" if decision.consistency == "SERIAL" else "triad",
        )
        evidence = [f"source:{sid}"]
        evidence.extend(
            citation.reference
            for turn in decision.examination.turns
            if turn.answer is not None
            for citation in turn.answer.citations
            if citation.reference.startswith(("source:", "brief:"))
        )
        ruling = Ruling(
            scope=[key],
            rendering=decision.term_rendering,
            source_quote=Quote(sentence_id=sid, text=sentence.text),
            rule=Rule(kind="term", parameters={"term": key}),
            rationale="Term selected through the configured resolution protocol",
            evidence=list(dict.fromkeys(evidence)),
            proposer=self.reviewer.agent,
            ratifier=actor.agent,
            consistency=decision.consistency,
            state="ratified",
            sentence_ids=list(sites),
        )
        event = record_triad_ruling(
            ledger,
            actor,
            identity,
            0,
            ruling,
            decision,
            agreement=self.agreement,
            voters=(task.actor.agent, self.peer.agent, self.reviewer.agent),
            reason=ruling.rationale,
            mode=task.resolution,
        )
        return event.seq if event.type == "Ratified" else None

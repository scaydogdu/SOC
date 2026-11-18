"""Run the same model triad with the RAG workflow's notes and repair selection."""

from pathlib import Path

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import Embedder, RetrievalConfig
from soc.enforce.checker import Checker
from soc.episode import Episode, load_runtime, reusable_term_sites, validate_runtime
from soc.ledger.models import Actor, CommitmentVersion, GateFinding, PrincipalAnswered
from soc.memory import TermNote
from soc.orchestrator.journal import Attempt
from soc.orchestrator.ledger_runner import ChunkTask, LedgerRunSpec
from soc.orchestrator.model_triad import (
    ModelTeam,
    ModelTriad,
    TriadCallFailed,
    TriadMemoryView,
    reusable_term_id,
)
from soc.orchestrator.prompts import digest, json_text
from soc.orchestrator.runner import (
    ExperimentSettings,
    RAGChunkTask,
    RunResult,
    run_episode,
)
from soc.principal.simulator import PrincipalSimulator, UnansweredQuestion
from soc.providers.client import CacheMiss, ModelClient
from soc.providers.models import Outcome, ProviderRequestError
from soc.triad.agreement import AgreementRules


class _RAGTriad:
    def __init__(
        self,
        triad: ModelTriad,
        memory: RAGMemory,
        episode: Episode,
        retrieval: RetrievalConfig,
        embedder: Embedder,
    ) -> None:
        self.triad = triad
        self.memory = memory
        self.episode = episode
        self.model_protocol = triad.configuration
        self.fingerprint = digest(
            json_text(
                [
                    triad.peer.model,
                    triad.reviewer.model,
                    triad.agreement.model_dump(mode="json"),
                    triad.principal.fingerprint,
                    triad.principal.actor.model_dump(mode="json"),
                    retrieval.model_dump(mode="json"),
                    f"{type(embedder).__module__}.{type(embedder).__qualname__}",
                ]
            )
        )

    def remember(self, attempt: Attempt) -> None:
        for term in attempt.terms:
            self.memory.remember_term(term)

    async def __call__(self, request: RAGChunkTask) -> Attempt:
        chunk = request.chunk
        actor = Actor(agent="executor", model=request.model, role="executor")
        notes = {
            item.ref.decision_id: CommitmentVersion(
                commitment_id=item.ref.decision_id,
                version=item.ref.version,
                valid_from=item.ref.version,
                scope=sorted(item.scope_keys),
                rendering=item.value,
                source_quote=None,
                rule=None,
                rationale="Current translation note",
                evidence=[],
                proposer=actor.agent,
                ratifier=actor.agent,
                consistency="SERIAL",
                state="ratified",
                strength=item.strength,
                sentence_ids=None
                if item.sentence_ids is None
                else sorted(item.sentence_ids),
            )
            for item in self.memory.decisions_for(chunk)
            if item.sentence_ids is None or item.sentence_ids
        }
        task = ChunkTask(
            f"chunk-{chunk.position}",
            chunk,
            actor,
            0,
            request.brief,
            notes,
            self.triad.spec.resolution,
            None,
            request.round_number,
            request.previous,
            tuple(
                GateFinding(
                    rule=finding.rule,
                    detail=finding.detail,
                    sentence_id=finding.sentence_id,
                    commitment_id=finding.decision_id,
                    strength=finding.strength,
                    outcome="unknown" if finding.status == "unknown" else "block",
                )
                for finding in request.findings
            ),
            (),
            request.context.text,
            None if request.handover is None else request.handover.text,
        )
        answers: list[PrincipalAnswered] = []
        usage: list[tuple[int, int, float]] = []
        revisions: list[dict[str, object]] = [
            {
                "used": {
                    "commitment_id": row.previous.ref.decision_id,
                    "version": row.previous.ref.version,
                },
                "current_rendering": row.current.value,
            }
            for row in request.revisions
        ]

        async def answer(question_id: str, question: str) -> str:
            result = await self.triad.principal.resolve(question_id, question)
            answers.append(result)
            return result.answer

        terms: list[TermNote] = []
        output = None
        try:
            result, _ = await self.triad.resolve(
                task,
                TriadMemoryView(
                    lambda: (request.brief, notes),
                    lambda _sid: revisions,
                    answer,
                    lambda _flag: None,
                ),
                usage,
            )
            outcome = Outcome.OK
            output = result.draft
            for decision in result.decisions:
                if decision.term is None or decision.term_rendering is None:
                    continue
                key = decision.term.scope_key
                sid = decision.examination.difference.sentence_id
                sites = reusable_term_sites(self.episode, sid, key)
                if sites:
                    terms.append(
                        TermNote(
                            decision_id=reusable_term_id(key, sid),
                            scope_key=key,
                            rendering=decision.term_rendering,
                            sentence_ids=sites,
                        )
                    )
        except TriadCallFailed as error:
            outcome = error.outcome
        except CacheMiss:
            outcome = Outcome.CACHE_MISS
        except (ProviderRequestError, UnansweredQuestion):
            outcome = Outcome.PARSE_FAILED
        return Attempt(
            outcome=outcome,
            output=output,
            input_tokens=sum(row[0] for row in usage),
            output_tokens=sum(row[1] for row in usage),
            estimated_cost_usd=sum(row[2] for row in usage),
            terms=terms,
            principal_answers=answers,
        )


async def run_model_rag_episode(
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
    embedder: Embedder,
    retrieval: RetrievalConfig | None = None,
) -> RunResult:
    """Use the model protocol with the baseline's retrieval and output-text search."""
    team.validate(spec, client)
    spec = spec.model_copy(update={"max_in_flight": 1})
    episode = validate_runtime(load_runtime(episode_dir))
    retrieval = retrieval or RetrievalConfig()
    memory = RAGMemory(embedder, checker, retrieval=retrieval, episode=episode)
    triad = ModelTriad(spec, experiment, client, team, agreement, principal, checker)
    return await run_episode(
        episode_dir,
        run_dir,
        spec=spec,
        experiment=experiment,
        memory=memory,
        checker=checker,
        client=client,
        triad_worker=_RAGTriad(triad, memory, episode, retrieval, embedder),
    )

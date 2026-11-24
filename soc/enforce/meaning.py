"""Shared, recorded resolution of undecided checks without evaluation keys."""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from soc.enforce.checker import Checker, Finding, NormativeRendering
from soc.episode import Brief, Sentence
from soc.ledger.models import (
    Actor,
    GateFinding,
    GateOutcome,
    HeldResolution,
    MeaningEvidence,
    Record,
    Text,
)
from soc.memory import ChunkOutput, Decision, ForbiddenRendering
from soc.providers.client import ModelClient
from soc.providers.models import Message, Outcome, Request


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class MeaningReply(Record):
    verdict: Literal["compliant", "violation", "unknown"]
    rationale: Text
    evidence: list[MeaningEvidence]


class HeldCheck(Record):
    context_id: Text
    chunk_id: Text
    build_number: int = Field(ge=1)
    source: Sentence
    translation: str
    brief: Brief
    finding: GateFinding
    requirements: list[str]

    @property
    def check_id(self) -> str:
        return digest(self.model_dump(mode="json"))


@dataclass(frozen=True)
class CheckAssessment:
    context_id: str
    must_checks: int
    held: tuple[HeldCheck, ...]


def gate_finding(finding: Finding) -> GateFinding:
    outcomes: dict[str, GateOutcome] = {
        "must": "block",
        "should": "warning",
        "may": "allowed",
    }
    outcome: GateOutcome = (
        ("held" if finding.strength == "must" else "unknown")
        if finding.status == "unknown"
        else outcomes[finding.strength]
    )
    return GateFinding(
        rule=finding.rule,
        detail=finding.detail,
        outcome=outcome,
        sentence_id=finding.sentence_id,
        commitment_id=finding.decision_id,
        strength=finding.strength,
        start=None if finding.span is None else finding.span.start,
        end=None if finding.span is None else finding.span.end,
        unknown_cause=finding.unknown_cause
        or ("runtime" if finding.status == "unknown" else None),
        brief_clause_ids=list(finding.brief_clause_ids),
    )


def assess(
    checker: Checker,
    chunk_id: str,
    build_number: int,
    source: Sequence[Sentence],
    output: ChunkOutput,
    brief: Brief,
    *,
    decisions: Sequence[Decision] = (),
    forbidden: Sequence[ForbiddenRendering] = (),
    normative: Sequence[NormativeRendering] = (),
) -> tuple[list[GateFinding], CheckAssessment]:
    def sites(scope: frozenset[str], declared: frozenset[str] | None) -> list[str]:
        return sorted(
            row.id
            for row in source
            if row.translatable
            and scope.intersection(row.scope_keys)
            and (declared is None or row.id in declared)
        )

    requirements = [
        canonical(
            {
                "id": row.ref.decision_id,
                "version": row.ref.version,
                "value": row.value,
                "kind": row.kind,
                "scope": sorted(row.scope_keys),
                "sites": sites(row.scope_keys, row.sentence_ids),
                "strength": row.strength,
            }
        )
        for row in decisions
        if sites(row.scope_keys, row.sentence_ids)
    ]
    requirements.extend(
        canonical(
            {
                "forbidden": row.rendering,
                "id": row.decision_id,
                "scope": sorted(row.scope_keys),
                "sites": sites(row.scope_keys, row.sentence_ids),
                "strength": row.strength,
            }
        )
        for row in forbidden
        if sites(row.scope_keys, row.sentence_ids)
    )
    requirements.extend(
        canonical(asdict(row))
        for row in normative
        if row.brief_clause_id in {clause.id for clause in brief.clauses}
    )
    requirements.sort()
    context = digest(
        [
            chunk_id,
            build_number,
            [row.model_dump(mode="json") for row in source],
            output.model_dump(mode="json"),
            brief.model_dump(mode="json"),
            requirements,
        ]
    )
    findings = [
        gate_finding(row)
        for row in checker.check(
            source,
            output,
            brief,
            decisions=decisions,
            forbidden=forbidden,
            normative=normative,
        )
    ]
    by_id = {row.id: row for row in source}
    texts = {row.sentence_id: row.text for row in output.sentences}
    checks = []
    for index, finding in enumerate(findings):
        if finding.outcome == "held" and finding.sentence_id in by_id:
            sid = finding.sentence_id
            assert sid is not None
            check = HeldCheck(
                context_id=context,
                chunk_id=chunk_id,
                build_number=build_number,
                source=by_id[sid],
                translation=texts.get(sid, ""),
                brief=brief,
                finding=finding,
                requirements=requirements,
            )
            checks.append(check)
            findings[index] = apply_resolution(finding, check, None)
    return findings, CheckAssessment(
        context,
        checker.count_must_checks(
            source,
            output,
            brief,
            decisions=decisions,
            forbidden=forbidden,
            normative=normative,
        ),
        tuple(checks),
    )


class MeaningResolver(Protocol):
    @property
    def actor(self) -> Actor: ...

    async def __call__(self, check: HeldCheck) -> MeaningReply | None: ...


def checked_reply(check: HeldCheck, reply: MeaningReply) -> MeaningReply:
    sources = {
        f"source:{check.source.id}": check.source.reading_text,
        f"output:{check.source.id}": check.translation,
    }
    sources.update(
        {f"brief:{clause.id}": clause.text for clause in check.brief.clauses}
    )
    if reply.verdict != "unknown" and (
        not reply.evidence
        or any(
            row.quote not in sources.get(row.reference, "") for row in reply.evidence
        )
    ):
        return MeaningReply(
            verdict="unknown",
            rationale="Meaning-check evidence could not be verified",
            evidence=[],
        )
    return reply


class MeaningUnavailable(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        super().__init__(outcome.value)


@dataclass(frozen=True)
class MeaningUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0


class ModelMeaningResolver:
    def __init__(
        self,
        client: ModelClient,
        model: str,
        effort: Literal["low", "high"],
        replicate: int,
        max_output_tokens: int,
    ) -> None:
        if model not in client.prices:
            raise ValueError("meaning-check model needs a configured price")
        self.client, self.model, self.effort = client, model, effort
        self.replicate, self.max_output_tokens = replicate, max_output_tokens
        self.actor = Actor(agent="meaning-check", model=model, role="resolver")
        self.usage: dict[str, MeaningUsage] = {}

    async def __call__(self, check: HeldCheck) -> MeaningReply | None:
        response = await self.client.generate(
            Request(
                model=self.model,
                effort=self.effort,
                replicate=self.replicate,
                run_id=self.client.run_id,
                call_id=f"meaning/{check.check_id}",
                schema=MeaningReply,
                sampling={"max_output_tokens": self.max_output_tokens},
                messages=(
                    Message(
                        role="system",
                        content="Check only the undecided requirement against the source, output and public brief. Return compliant, violation, or unknown with a short rationale. Cite exact quotes using source:<sentence_id>, output:<sentence_id> or brief:<clause_id>. Use unknown when the supplied evidence does not determine the requirement.",
                    ),
                    Message(
                        role="user",
                        content=canonical(
                            check.model_dump(mode="json", exclude={"brief"})
                            | {"brief": check.brief.public_data()}
                        ),
                    ),
                ),
            ),
            recover=True,
        )
        self.usage[check.check_id] = MeaningUsage(
            response.reply.input_tokens,
            response.reply.output_tokens,
            response.estimated_cost_usd,
        )
        if response.parsed is None:
            raise MeaningUnavailable(response.reply.outcome)
        return response.parsed


class MeaningStore:
    """Append-only results and check opportunities, shared by both memory arms."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS resolutions (check_id TEXT PRIMARY KEY, data TEXT NOT NULL CHECK(json_valid(data))) STRICT;
            CREATE TABLE IF NOT EXISTS checks (context_id TEXT PRIMARY KEY, must_checks INTEGER NOT NULL CHECK(must_checks>=0), held TEXT NOT NULL CHECK(json_valid(held))) STRICT;
            CREATE TRIGGER IF NOT EXISTS resolutions_no_update BEFORE UPDATE ON resolutions BEGIN SELECT RAISE(ABORT,'meaning records are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS resolutions_no_delete BEFORE DELETE ON resolutions BEGIN SELECT RAISE(ABORT,'meaning records are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS checks_no_update BEFORE UPDATE ON checks BEGIN SELECT RAISE(ABORT,'meaning records are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS checks_no_delete BEFORE DELETE ON checks BEGIN SELECT RAISE(ABORT,'meaning records are append-only'); END;
        """)
        return db

    def observe(
        self, context_id: str, must_checks: int, held: Sequence[HeldCheck]
    ) -> None:
        rows = [
            {"check_id": row.check_id, "cause": row.finding.unknown_cause or "runtime"}
            for row in held
        ]
        value = (must_checks, canonical(rows))
        with closing(self._open()) as db, db:
            previous = db.execute(
                "SELECT must_checks, held FROM checks WHERE context_id=?", (context_id,)
            ).fetchone()
            if previous is not None and previous != value:
                raise ValueError("check audit context changed")
            db.execute(
                "INSERT OR IGNORE INTO checks VALUES (?,?,?)", (context_id, *value)
            )

    async def resolve(
        self, check: HeldCheck, resolver: MeaningResolver | None
    ) -> HeldResolution | None:
        if resolver is None:
            return None
        with closing(self._open()) as db:
            previous = db.execute(
                "SELECT data FROM resolutions WHERE check_id=?", (check.check_id,)
            ).fetchone()
        if previous is not None:
            result = HeldResolution.model_validate_json(previous[0])
            if (
                result.resolver != resolver.actor
                or result.check_id != check.check_id
                or result.chunk_id != check.chunk_id
                or result.build_number != check.build_number
            ):
                raise ValueError("meaning resolver changed for a recorded check")
            return result
        reply = await resolver(check)
        if reply is None:
            return None
        reply = checked_reply(check, reply)
        result = HeldResolution(
            check_id=check.check_id,
            chunk_id=check.chunk_id,
            build_number=check.build_number,
            resolver=resolver.actor,
            **(
                asdict(resolver.usage[check.check_id])
                if isinstance(resolver, ModelMeaningResolver)
                else {}
            ),
            **reply.model_dump(),
        )
        with closing(self._open()) as db, db:
            db.execute(
                "INSERT INTO resolutions VALUES (?,?)",
                (check.check_id, result.model_dump_json()),
            )
        return result

    def results(self) -> list[HeldResolution]:
        with closing(self._open()) as db:
            return [
                HeldResolution.model_validate_json(row[0])
                for row in db.execute("SELECT data FROM resolutions ORDER BY check_id")
            ]

    def usage(self, resolver: MeaningResolver | None) -> MeaningUsage:
        values = {
            row.check_id: MeaningUsage(
                row.input_tokens, row.output_tokens, row.estimated_cost_usd
            )
            for row in self.results()
        }
        if isinstance(resolver, ModelMeaningResolver):
            values.update(resolver.usage)
        return MeaningUsage(
            sum(row.input_tokens for row in values.values()),
            sum(row.output_tokens for row in values.values()),
            sum(row.estimated_cost_usd for row in values.values()),
        )

    def report(
        self, *, limit: float = 0.01, unverified_final: int | None = None
    ) -> dict[str, object]:
        with closing(self._open()) as db:
            contexts = db.execute("SELECT must_checks, held FROM checks").fetchall()
        opportunities = sum(row[0] for row in contexts)
        held = {
            row["check_id"]: row["cause"]
            for _, data in contexts
            for row in json.loads(data)
        }
        results = {
            row.check_id: row.verdict for row in self.results() if row.check_id in held
        }
        closed = sum(value in {"compliant", "violation"} for value in results.values())
        configuration = sum(cause == "configuration" for cause in held.values())
        runtime = len(held) - configuration
        return {
            "must_checks": opportunities,
            "unknown_must": len(held),
            "configuration_unknowns": configuration,
            "runtime_unknowns": runtime,
            "unknown_rate": len(held) / opportunities if opportunities else None,
            "runtime_unknown_rate": runtime / opportunities if opportunities else None,
            "resolved": closed,
            "compliant": sum(value == "compliant" for value in results.values()),
            "violation": sum(value == "violation" for value in results.values()),
            "unverified": len(held) - closed,
            "unverified_final": len(held) - closed
            if unverified_final is None
            else unverified_final,
            "runtime_rate_limit": limit,
            "pilot_b_unknowns_pass": bool(
                opportunities
                and configuration == 0
                and runtime / opportunities <= limit
            ),
        }


def apply_resolution(
    finding: GateFinding, check: HeldCheck, resolution: HeldResolution | None
) -> GateFinding:
    outcome = (
        "held"
        if resolution is None or resolution.verdict == "unknown"
        else ("allowed" if resolution.verdict == "compliant" else "block")
    )
    detail = (
        finding.detail
        if resolution is None or resolution.verdict == "unknown"
        else f"{finding.detail}; meaning check {resolution.verdict}: {resolution.rationale}"
    )
    return finding.model_copy(
        update={"outcome": outcome, "check_id": check.check_id, "detail": detail}
    )

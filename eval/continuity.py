"""Apply the unverified-site exclusion to independently evaluated continuity uses."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from soc.episode import NonBlank, Record


class UseAssessment(Record):
    decision_id: NonBlank
    sentence_id: NonBlank
    chunk: int = Field(ge=1)
    kept: bool


class PublishedFinding(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    sentence_id: str | None = None
    commitment_id: str | None = None
    decision_id: str | None = None
    outcome: str | None = None
    status: str | None = None
    strength: str | None = None

    @property
    def unverified(self) -> bool:
        return (
            self.outcome in {"held", "unverified"}
            or self.status == "unverified"
            or (
                (self.status == "unknown" or self.outcome == "unknown")
                and self.strength not in {"should", "may"}
            )
        )


class PublishedChunk(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    chunk: int = Field(ge=1)
    findings: list[PublishedFinding]


def score_continuity(
    assessments: Sequence[UseAssessment], published: Sequence[PublishedChunk]
) -> dict[str, object]:
    """Preserve independent judgments; unverified is a veto, never a positive label.

    Sentence-wide findings without a decision ID conservatively exclude every
    assessed use on that sentence. Rejected/unfinished output is still scored.
    This is the continuity aggregation boundary, not the still-pending all-seed
    source-fidelity, client-preference or judge-panel evaluator.
    """
    identities = {(row.decision_id, row.chunk, row.sentence_id) for row in assessments}
    if len(identities) != len(assessments):
        raise ValueError("continuity assessments must identify unique use sites")
    blocked = [
        (row.chunk, finding)
        for row in published
        for finding in row.findings
        if finding.unverified
    ]
    uses = []
    present = {row.chunk for row in published}
    for row in assessments:
        unverified = any(
            chunk == row.chunk
            and (finding.sentence_id is None or finding.sentence_id == row.sentence_id)
            and (finding.commitment_id or finding.decision_id)
            in {None, row.decision_id}
            for chunk, finding in blocked
        )
        missing = row.chunk not in present
        uses.append(
            {
                **row.model_dump(),
                "kept": row.kept and not unverified and not missing,
                "unverified": unverified,
                "missing": missing,
            }
        )
    kept = sum(bool(row["kept"]) for row in uses)
    return {
        "uses": uses,
        "total": len(uses),
        "kept": kept,
        "unverified": sum(bool(row["unverified"]) for row in uses),
        "missing": sum(bool(row["missing"]) for row in uses),
        "continuity": kept / len(uses) if uses else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessments", type=Path)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    judgments = [
        UseAssessment.model_validate_json(line)
        for line in args.assessments.read_text().splitlines()
    ]
    published = [
        PublishedChunk.model_validate_json(line)
        for line in (args.run / "final_findings.jsonl").read_text().splitlines()
    ]
    print(
        json.dumps(score_continuity(judgments, published), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()

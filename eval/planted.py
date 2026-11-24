"""Private, opt-in planted-error control and mandatory reversion before scoring."""

import argparse
import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from soc.episode import NonBlank, Record
from soc.memory import ChunkOutput, Translation


def _hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class NaturalError(Record):
    id: NonBlank
    chunk: int = Field(ge=1)
    kind: NonBlank
    runtime_ref: NonBlank


class Plant(Record):
    id: NonBlank
    chunk: int = Field(ge=1)
    sentence_id: NonBlank
    kind: NonBlank
    natural_error_id: NonBlank
    original: NonBlank
    planted: NonBlank
    worker: NonBlank = "executor"
    round_number: int = Field(default=0, ge=0)


class PlantPlan(Record):
    observations: list[NaturalError]
    plants: list[Plant]

    @model_validator(mode="after")
    def matched(self) -> Self:
        observed = {row.id: row for row in self.observations}
        if len(observed) != len(self.observations):
            raise ValueError("natural observations require unique IDs")
        identities = {
            (row.chunk, row.round_number, row.worker, row.sentence_id)
            for row in self.plants
        }
        if len(identities) != len(self.plants) or len(
            {row.id for row in self.plants}
        ) != len(self.plants):
            raise ValueError(
                "plants require unique IDs and one edit per draft sentence"
            )
        for row in self.plants:
            basis = observed.get(row.natural_error_id)
            if basis is None or (basis.chunk, basis.kind) != (row.chunk, row.kind):
                raise ValueError(
                    "plant needs an observed natural-error kind in the same chunk"
                )
            if (
                row.original == row.planted
                or row.planted in row.original
                or row.original in row.planted
            ):
                raise ValueError("plant spans must be distinct and non-nested")
            if any(
                row.planted in other.planted
                or row.planted in other.original
                or other.original in row.planted
                for other in self.plants
                if other.id != row.id
            ):
                raise ValueError(
                    "plant spans must not overlap or cascade across records"
                )
        return self


class Exposure(Record):
    chunk: int
    round_number: int
    worker: str
    plant_ids: list[str]
    before: ChunkOutput
    after: ChunkOutput


class PlantSession:
    """An evaluator-owned controller. Runtime receives only its narrow protocol.

    No automatic share or error synthesis is selected: a private, authored plan
    names exact span replacements, supported by same-chunk natural observations.
    Unknown paraphrases or copied probes fail closed rather than being scored.
    """

    def __init__(
        self,
        private_dir: Path,
        run_id: str,
        plan: PlantPlan,
        *,
        public_roots: tuple[Path, ...],
    ) -> None:
        directory = private_dir.resolve()
        if not public_roots or any(
            directory.is_relative_to(root.resolve())
            or root.resolve().is_relative_to(directory)
            for root in public_roots
        ):
            raise ValueError(
                "private records must be separate from public, run and prompt roots"
            )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "plants.sqlite"
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("private plant database must be a regular file")
        self.plan = PlantPlan.model_validate_json(plan.model_dump_json())
        self.fingerprint = _hash(plan.model_dump_json())
        self.binding = json.dumps(
            [run_id, plan.model_dump(mode="json")], ensure_ascii=False, sort_keys=True
        )
        with closing(sqlite3.connect(self.path)) as db:
            self.path.chmod(0o600)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL) STRICT;
                CREATE TABLE IF NOT EXISTS exposures (key TEXT PRIMARY KEY, value TEXT NOT NULL CHECK(json_valid(value))) STRICT;
                CREATE TRIGGER IF NOT EXISTS binding_no_update BEFORE UPDATE ON binding BEGIN SELECT RAISE(ABORT, 'plants are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS binding_no_delete BEFORE DELETE ON binding BEGIN SELECT RAISE(ABORT, 'plants are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS exposures_no_update BEFORE UPDATE ON exposures BEGIN SELECT RAISE(ABORT, 'plants are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS exposures_no_delete BEFORE DELETE ON exposures BEGIN SELECT RAISE(ABORT, 'plants are append-only'); END;
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                stored = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
                if stored is None:
                    db.execute("INSERT INTO binding VALUES (1,?)", (self.binding,))
                elif stored[0] != self.binding:
                    raise ValueError("private plant binding changed")

    def _expose(
        self, chunk: int, round_number: int, worker: str, output: ChunkOutput
    ) -> Exposure:
        edits = [
            row
            for row in self.plan.plants
            if (row.chunk, row.round_number, row.worker)
            == (chunk, round_number, worker)
        ]
        sentences = {row.sentence_id: row.text for row in output.sentences}
        if len(sentences) != len(output.sentences):
            raise ValueError("plant span requires unique sentence IDs")
        for edit in edits:
            text = sentences.get(edit.sentence_id, "")
            if text.count(edit.original) != 1 or any(
                edit.planted in value for value in sentences.values()
            ):
                raise ValueError("plant span is missing, ambiguous or already present")
            sentences[edit.sentence_id] = text.replace(edit.original, edit.planted, 1)
        after = ChunkOutput(
            sentences=[
                Translation(
                    sentence_id=row.sentence_id, text=sentences[row.sentence_id]
                )
                for row in output.sentences
            ]
        )
        return Exposure(
            chunk=chunk,
            round_number=round_number,
            worker=worker,
            plant_ids=[row.id for row in edits],
            before=output,
            after=after,
        )

    def apply(
        self, chunk: int, round_number: int, worker: str, output: ChunkOutput
    ) -> ChunkOutput:
        self._active()
        exposure = self._expose(chunk, round_number, worker, output)
        if not exposure.plant_ids:
            return output
        key = json.dumps([chunk, round_number, worker])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute(
                "SELECT value FROM exposures WHERE key=?", (key,)
            ).fetchone()
            raw = exposure.model_dump_json()
            if stored is None:
                db.execute("INSERT INTO exposures VALUES (?,?)", (key, raw))
            elif stored[0] != raw:
                raise ValueError("private plant draft changed on resume")
        return exposure.after

    def _active(self) -> list[Plant]:
        with closing(sqlite3.connect(self.path)) as db:
            binding = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
            if binding != (self.binding,):
                raise ValueError("private plant binding changed")
            records = db.execute(
                "SELECT key,value FROM exposures ORDER BY key"
            ).fetchall()
        active: set[str] = set()
        for key, raw in records:
            row = Exposure.model_validate_json(raw)
            expected = self._expose(row.chunk, row.round_number, row.worker, row.before)
            if (
                key != json.dumps([row.chunk, row.round_number, row.worker])
                or row != expected
            ):
                raise ValueError("private plant exposure changed")
            active.update(row.plant_ids)
        return [row for row in self.plan.plants if row.id in active]

    def revert(self, outputs: Mapping[int, ChunkOutput]) -> dict[int, ChunkOutput]:
        clean = {
            chunk: {(chunk, row.sentence_id): row.text for row in output.sentences}
            for chunk, output in outputs.items()
        }
        flat = {key: text for rows in clean.values() for key, text in rows.items()}
        if len(flat) != sum(len(output.sentences) for output in outputs.values()):
            raise ValueError("reversion requires unique sentence IDs per chunk")
        active = self._active()
        for plant in active:
            target = (plant.chunk, plant.sentence_id)
            matches = [
                (key, text.count(plant.planted))
                for key, text in flat.items()
                if plant.planted in text
            ]
            if matches:
                if matches != [(target, 1)] or plant.original in flat[target]:
                    raise ValueError(
                        "reversion refused ambiguous or propagated planted span"
                    )
                flat[target] = flat[target].replace(plant.planted, plant.original, 1)
            elif target in flat and flat[target].count(plant.original) != 1:
                raise ValueError("reversion cannot verify a rewritten planted span")
        if any(plant.planted in text for plant in active for text in flat.values()):
            raise ValueError("reversion left a planted span")
        return {
            chunk: ChunkOutput(
                sentences=[
                    Translation(
                        sentence_id=row.sentence_id, text=flat[chunk, row.sentence_id]
                    )
                    for row in output.sentences
                ]
            )
            for chunk, output in outputs.items()
        }

    def score[T](
        self,
        outputs: Mapping[int, ChunkOutput],
        scorer: Callable[[dict[int, ChunkOutput]], T],
    ) -> T:
        """The scorer only receives restored translations, without control metadata."""
        return scorer(self.revert(outputs))


def read_outputs(path: Path) -> dict[int, ChunkOutput]:
    outputs: dict[int, ChunkOutput] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        chunk = row.pop("chunk")
        if type(chunk) is not int or chunk < 1 or chunk in outputs:
            raise ValueError("reversion requires unique one-based chunk positions")
        outputs[chunk] = ChunkOutput.model_validate(row)
    return outputs


def load_session(
    private: Path, run_id: str, *, public_roots: tuple[Path, ...]
) -> PlantSession:
    with closing(
        sqlite3.connect(
            f"file:{(private / 'plants.sqlite').resolve()}?mode=ro", uri=True
        )
    ) as db:
        (raw,) = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
    bound_run, config = json.loads(raw)
    if bound_run != run_id:
        raise ValueError("private plant binding belongs to another run")
    return PlantSession(
        private,
        run_id,
        PlantPlan.model_validate_json(json.dumps(config)),
        public_roots=public_roots,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private", type=Path)
    parser.add_argument("input", type=Path, help="raw final_chunks.jsonl")
    parser.add_argument("output", type=Path, help="new, restored JSONL for evaluation")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or args.output.exists():
        raise ValueError("reversion must write a new output and preserve raw artifacts")
    # The private database is never copied into a runtime or scoring directory.
    session = load_session(
        args.private, args.run_id, public_roots=(args.input.parent, args.output.parent)
    )
    restored = session.revert(read_outputs(args.input))
    content = "".join(
        json.dumps(
            {"chunk": chunk, **output.model_dump(mode="json")}, ensure_ascii=False
        )
        + "\n"
        for chunk, output in sorted(restored.items())
    )
    # Exclusive creation also protects against another process publishing this path.
    with args.output.open("x", encoding="utf-8") as target:
        target.write(content)


if __name__ == "__main__":
    main()

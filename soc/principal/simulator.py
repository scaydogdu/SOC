"""Answer individual questions from an isolated key and cache answers across runs."""

import hashlib
import json
import sqlite3
import unicodedata
from asyncio import Lock
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path

from soc.episode import BriefChange, NonBlank, Record, read_yaml
from soc.ledger.models import Actor, BriefChanged, Event, PrincipalAnswered
from soc.ledger.store import Ledger
from soc.providers.client import configured_credentials, ensure_no_credentials

type Fallback = Callable[[str], Awaitable[str | None]]


class AnswerKey(Record):
    answers: dict[NonBlank, NonBlank]


class UnansweredQuestion(LookupError):
    """The key and fallback have no answer to the requested question."""


def normalize_question(question: str) -> str:
    normal = " ".join(unicodedata.normalize("NFKC", question).casefold().split())
    if not normal:
        raise ValueError("principal question cannot be blank")
    return normal


class PrincipalSimulator:
    """One episode's principal. Reuse a cache path across experimental conditions."""

    def __init__(
        self,
        key_path: Path,
        cache_path: Path,
        *,
        episode_id: str,
        actor: Actor,
        fallback: Fallback | None = None,
    ) -> None:
        if actor.role != "principal" or not episode_id.strip():
            raise ValueError("a principal actor and episode ID are required")
        key = read_yaml(key_path, AnswerKey)
        self._answers = {
            normalize_question(q): answer for q, answer in key.answers.items()
        }
        if len(self._answers) != len(key.answers):
            raise ValueError("answer key has duplicate normalized questions")
        encoded = json.dumps(self._answers, sort_keys=True, ensure_ascii=False)
        ensure_no_credentials(configured_credentials(), encoded)
        self.fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        self._path = cache_path
        self._episode_id = episode_id
        self._actor = actor
        self._fallback = fallback
        self._lock = Lock()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cache_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS answer_keys (episode TEXT PRIMARY KEY, sha256 TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO answer_keys VALUES (?, ?)",
                (episode_id, self.fingerprint),
            )
            prior = db.execute(
                "SELECT sha256 FROM answer_keys WHERE episode = ?", (episode_id,)
            ).fetchone()
            if prior[0] != self.fingerprint:
                raise ValueError(
                    "the answer key changed for this episode's shared cache"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS answers ("
                "episode TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, "
                "PRIMARY KEY (episode, question))"
            )

    @property
    def actor(self) -> Actor:
        return self._actor

    def _check_principal(self, ledger: Ledger) -> None:
        if ledger.authority is None or ledger.authority.principal != self._actor.agent:
            raise ValueError("the simulator must be the registered operation principal")

    async def answer(
        self, ledger: Ledger, question_id: str, question: str, *, chunk_id: str
    ) -> str:
        """Log each answered logical request, including answers served from cache."""
        self._check_principal(ledger)
        normalized = normalize_question(question)
        ensure_no_credentials(configured_credentials(), question)
        identity = (self._episode_id, normalized)
        async with self._lock:
            with closing(sqlite3.connect(self._path)) as db:
                row = db.execute(
                    "SELECT answer FROM answers WHERE episode = ? AND question = ?",
                    identity,
                ).fetchone()
                if row is None:
                    answer = self._answers.get(normalized)
                    if answer is None and self._fallback is not None:
                        answer = await self._fallback(normalized)
                    if answer is None or not answer.strip():
                        raise UnansweredQuestion(normalized)
                    ensure_no_credentials(configured_credentials(), answer)
                    with db:
                        db.execute(
                            "INSERT OR IGNORE INTO answers VALUES (?, ?, ?)",
                            (*identity, answer),
                        )
                        row = db.execute(
                            "SELECT answer FROM answers WHERE episode = ? AND question = ?",
                            identity,
                        ).fetchone()
                assert row is not None
                response: str = row[0]
        ledger.record(
            self._actor,
            PrincipalAnswered(
                question_id=question_id,
                answer=response,
                question_hash=hashlib.sha256(normalized.encode()).hexdigest(),
            ),
            chunk_id=chunk_id,
        )
        return response

    def issue_brief_change(
        self, ledger: Ledger, change: BriefChange, *, after_chunk: int
    ) -> Event:
        """Issue the public scheduled clauses through the principal's ledger authority."""
        self._check_principal(ledger)
        return ledger.record(
            self._actor,
            BriefChanged(
                clauses={clause.id: clause.text for clause in change.clauses},
                after_chunk=after_chunk,
            ),
        )

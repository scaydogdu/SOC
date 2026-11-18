import os
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from soc.ledger.models import BriefChanged, ChunkBuilt, VersionRef
from soc.ledger.store import AuthorityError, Ledger, Projection
from tests.ledger_examples import IDENTITY, OTHER, OWNER, RULING

IDENTITIES = {
    key: IDENTITY.model_copy(update={"id": key}) for key in ("D1", "D2", "D3")
}
SCOPES = ("sec-A", "sec-B", "sec-C")
RENDERINGS = ("belirteç", "jeton", "token")
IDS = st.sampled_from(tuple(IDENTITIES))
RENDERING = st.sampled_from(RENDERINGS)
SCOPE = st.lists(st.sampled_from(SCOPES), min_size=1, max_size=3, unique=True)


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "state.sqlite"
        self.ledger = Ledger(path, "state")
        self.peer = Ledger(path, "state")
        self.reader = self.ledger.reader()
        self.current = {}
        self.history = {}
        self.tombstones = []
        self.chunks = {}
        self.brief = {}
        self.seq = 0

    def teardown(self):
        try:
            self.check_projections_and_lookups()
        finally:
            self.peer.__exit__()
            self.ledger.__exit__()
            self.directory.cleanup()

    def change(self, kind, key, rendering, scope):
        old_version, old_rendering, old_scope, old_state = self.current.get(
            key, (0, "", set(), "revoked")
        )
        state = {"Revised": "amended", "Revoked": "revoked"}.get(kind, "ratified")
        ruling = RULING.model_copy(
            update={"state": state, "rendering": rendering, "scope": scope}
        )
        if kind == "Ratified":
            event = self.ledger.ratify(
                OWNER, IDENTITIES[key], old_version, ruling, "ratify"
            )
        else:
            event = self.ledger.transition(
                kind, OWNER, key, old_version, ruling, "change"
            )
        self.seq += 1
        assert (event.type, event.new_version, event.seq) == (
            kind,
            old_version + 1,
            self.seq,
        )
        lost = {
            item
            for item in old_scope
            if old_state != "revoked"
            and not (
                state != "revoked" and old_rendering == rendering and item in scope
            )
        }
        if lost:
            self.seq += 1
            self.tombstones.append((self.seq, key, old_version, old_rendering, lost))
        self.current[key] = (old_version + 1, rendering, set(scope), state)
        self.history[event.seq] = dict(self.current)

    @rule(key=IDS, rendering=RENDERING, scope=SCOPE)
    def ratify(self, key, rendering, scope):
        self.change("Ratified", key, rendering, scope)

    @precondition(lambda self: bool(self.current))
    @rule(
        data=st.data(),
        kind=st.sampled_from(("Revised", "Superseded", "Revoked")),
        rendering=RENDERING,
        scope=SCOPE,
    )
    def transition(self, data, kind, rendering, scope):
        key = data.draw(st.sampled_from(tuple(self.current)))
        self.change(kind, key, rendering, scope)

    @precondition(lambda self: bool(self.current))
    @rule(data=st.data())
    def unauthorized(self, data):
        key = data.draw(st.sampled_from(tuple(self.current)))
        with pytest.raises(AuthorityError):
            self.ledger.transition(
                "Revised", OTHER, key, self.current[key][0], RULING, "unauthorized"
            )

    @rule(value=st.integers())
    def brief_change(self, value):
        self.ledger.record(OWNER, BriefChanged(clauses={"B1": str(value)}))
        self.seq += 1
        self.brief["B1"] = str(value)

    @rule(key=IDS, rendering=RENDERING, scope=SCOPE)
    def concurrent_stale_write(self, key, rendering, scope):
        visible = self.peer.snapshot().current.get(key)
        base = visible.version if visible is not None else 0
        self.change("Ratified", key, rendering, scope)
        event = self.peer.ratify(OWNER, IDENTITIES[key], base, RULING, "stale")
        self.seq += 1
        assert event.type == "ConflictRaised"
        assert event.payload["data"] == {
            "expected": base,
            "actual": self.current[key][0],
        }

    @rule(scope=st.sampled_from(SCOPES))
    def build_chunk(self, scope):
        active = {
            key: (version, rendering)
            for key, (version, rendering, scopes, state) in self.current.items()
            if scope in scopes and state != "revoked"
        }
        chunk = ChunkBuilt(
            ledger_version=self.seq,
            uses=[
                VersionRef(commitment_id=key, version=entry[0])
                for key, entry in active.items()
            ],
            decisions={key: entry[1] for key, entry in active.items()},
            output_ref=f"outputs/chunk-{self.seq + 1}/build-1.json",
        )
        self.seq += 1
        chunk_id = f"chunk-{self.seq}"
        self.ledger.record(OWNER, chunk, chunk_id=chunk_id)
        self.chunks[chunk_id] = chunk

    @invariant()
    def current_lookups_match_model(self):
        for scope in SCOPES:
            entries = self.reader.in_scope(scope, at=self.seq)
            assert {
                key: (item.version, item.rendering, set(item.scope), item.state)
                for key, item in entries.items()
            } == {
                key: entry
                for key, entry in self.current.items()
                if scope in entry[2] and entry[3] != "revoked"
            }
        for rendering, scope in product(RENDERINGS, SCOPES):
            current = any(
                value == rendering and scope in scopes and state != "revoked"
                for _, value, scopes, state in self.current.values()
            )
            lost = any(
                value == rendering and scope in scopes
                for _, _, _, value, scopes in self.tombstones
            )
            assert self.ledger.is_tombstoned(rendering, scope) == (lost and not current)

    def check_projections_and_lookups(self):
        view = self.ledger.snapshot()
        assert view == self.ledger.rebuild()
        assert self.ledger.verify() is None
        assert view.ledger_version == self.seq
        assert view.brief == self.brief
        assert view.chunks == self.chunks
        events = self.ledger._events()
        actual_tombstones = [
            (
                event.seq,
                event.commitment_id,
                tomb.version,
                tomb.rendering,
                set(tomb.scope),
            )
            for event, tomb in zip(
                (event for event in events if event.type == "Tombstoned"),
                view.tombstones,
                strict=True,
            )
        ]
        assert actual_tombstones == self.tombstones

        reader = self.ledger.reader()
        prefix = Projection()
        expected = {}
        for at in range(self.seq + 1):
            if at:
                prefix._apply(events[at - 1])
                expected = self.history.get(at, expected)
            for scope in SCOPES:
                entries = reader.in_scope(scope, at=at)
                assert entries == {
                    key: value
                    for key, value in prefix.current.items()
                    if scope in value.scope and value.state != "revoked"
                }
                assert {
                    key: (item.version, item.rendering, set(item.scope), item.state)
                    for key, item in entries.items()
                } == {
                    key: entry
                    for key, entry in expected.items()
                    if scope in entry[2] and entry[3] != "revoked"
                }
        if self.seq:
            with pytest.raises(ValueError, match="go backwards"):
                reader.in_scope(SCOPES[0], at=self.seq - 1)


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(
    max_examples=int(os.environ.get("SOC_LEDGER_EXAMPLES", "35")),
    stateful_step_count=12,
    deadline=None,
    database=None,
    derandomize=os.environ.get("SOC_LEDGER_RANDOM") != "1",
)

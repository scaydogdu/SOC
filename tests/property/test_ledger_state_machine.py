import os
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from soc.ledger.models import BriefChanged
from soc.ledger.store import Ledger
from tests.ledger_examples import IDENTITY, OWNER, RULING


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "state.sqlite"
        self.ledger = Ledger(path, "state")
        self.peer = Ledger(path, "state")
        self.version = 0
        self.seq = 0
        self.rendering = ""
        self.state = ""
        self.tombstones = []
        self.brief = {}

    def teardown(self):
        self.peer.__exit__()
        self.ledger.__exit__()
        self.directory.cleanup()

    def change(self, kind, suffix=0):
        state = {"Revised": "amended", "Revoked": "revoked"}.get(kind, "ratified")
        rendering = f"rendering {self.version + 1}/{suffix}"
        ruling = RULING.model_copy(update={"state": state, "rendering": rendering})
        if kind == "Ratified":
            event = self.ledger.ratify(OWNER, IDENTITY, self.version, ruling, "ratify")
        else:
            event = self.ledger.transition(
                kind, OWNER, IDENTITY.id, self.version, ruling, "change"
            )
        self.version += 1
        self.seq += 1
        assert (
            event.type == kind
            and event.new_version == self.version
            and event.seq == self.seq
        )
        if kind == "Superseded":
            self.tombstones.append(self.rendering)
            self.seq += 1
        self.rendering = rendering
        self.state = state

    @rule(suffix=st.integers(min_value=0, max_value=2**32 - 1))
    def ratify(self, suffix):
        self.change("Ratified", suffix)

    @precondition(lambda self: self.version > 0)
    @rule()
    def revise(self):
        self.change("Revised")

    @precondition(lambda self: self.version > 0)
    @rule()
    def supersede(self):
        self.change("Superseded")

    @precondition(lambda self: self.version > 0)
    @rule()
    def revoke(self):
        self.change("Revoked")

    @rule()
    def brief_change(self):
        value = str(self.seq)
        self.ledger.record(OWNER, BriefChanged(clauses={"B1": value}))
        self.seq += 1
        self.brief["B1"] = value

    @rule()
    def concurrent_stale_write(self):
        visible = self.peer.snapshot().current.get(IDENTITY.id)
        base = visible.version if visible is not None else 0
        self.change("Ratified")
        event = self.peer.ratify(OWNER, IDENTITY, base, RULING, "stale")
        self.seq += 1
        assert event.type == "ConflictRaised"
        assert event.payload["data"] == {"expected": base, "actual": self.version}

    @invariant()
    def projections_and_chain(self):
        view = self.ledger.snapshot()
        assert view == self.ledger.rebuild()
        assert self.ledger.verify() is None
        assert view.ledger_version == self.seq
        assert view.brief == self.brief
        assert [t.rendering for t in view.tombstones] == self.tombstones
        assert len(view.versions) == self.version
        if self.version:
            current = view.current[IDENTITY.id]
            assert (current.version, current.rendering, current.state) == (
                self.version,
                self.rendering,
                self.state,
            )


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(
    max_examples=int(os.environ.get("SOC_LEDGER_EXAMPLES", "35")),
    stateful_step_count=6,
    deadline=None,
    database=None,
    derandomize=os.environ.get("SOC_LEDGER_RANDOM") != "1",
)

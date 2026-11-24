"""Optional draft transformation boundary; private controllers live outside runtime.

Only the returned translation enters normal review, gate and repair paths.
The opaque fingerprint binds resumption; no annotation or path enters a prompt.
"""

from typing import Protocol

from soc.memory import ChunkOutput


class DraftIntervention(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def apply(
        self, chunk: int, round_number: int, worker: str, output: ChunkOutput
    ) -> ChunkOutput: ...

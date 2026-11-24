"""Reject deterministic checker configuration gaps before spending on an episode."""

from collections.abc import Sequence

from soc.episode import Episode, EpisodeError
from soc.tasks.segmenter import BCP14


def validate_checkability(
    episode: Episode, *, normative: Sequence[object] = ()
) -> None:
    mappings = (*episode.brief.normative, *normative)
    for decision in episode.events.approvals:
        if (
            decision.kind not in {"term", "non_lexical", "scope_limited"}
            or not decision.value.strip()
        ):
            raise EpisodeError(
                f"decision {decision.decision_id}: missing checkable wording or deterministic rule"
            )
    source = {row.id: row for row in episode.source}
    for position, chunk in enumerate(episode.events.chunks, 1):
        clauses = {clause.id for clause in episode.brief.clauses}
        if position > episode.events.brief_change.chunk:
            clauses.update(clause.id for clause in episode.events.brief_change.clauses)
        for sid in chunk.sentence_ids:
            sentence = source[sid]
            if not sentence.translatable:
                continue
            for keyword in sorted(set(BCP14.findall(sentence.reading_text))):
                candidates = {
                    (
                        getattr(rule, "value", ""),
                        getattr(rule, "strength", "must"),
                        getattr(rule, "brief_clause_id", None),
                    )
                    for rule in mappings
                    if getattr(rule, "keyword", None) == keyword
                    and getattr(rule, "brief_clause_id", None) in clauses
                    and getattr(rule, "value", "").strip()
                }
                if len(candidates) != 1:
                    decisions = sorted(
                        row.decision_id
                        for row in episode.events.approvals
                        if keyword in row.scope_keys
                    )
                    identifier = ",".join(decisions) or f"source:{sid}"
                    raise EpisodeError(
                        f"decision {identifier}: missing or ambiguous approved modal wording for {keyword} at {sid}"
                    )

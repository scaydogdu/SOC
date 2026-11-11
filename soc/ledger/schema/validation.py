"""Produce local JSON-LD records and validate their graph using SHACL."""

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from time import perf_counter_ns
from typing import cast
from urllib.parse import quote

from pydantic import JsonValue
from pyshacl import validate
from rdflib import Graph
from rdflib.namespace import SH

from soc.ledger.models import Actor, Commitment, CommitmentVersion, ExecutorRebound

ASSETS = Path(__file__).parent
NAMESPACE = "https://w3id.org/soc-testbed#"
Node = dict[str, JsonValue]


class ShapeError(ValueError):
    """The write did not conform to the commitment shapes."""


@cache
def _context() -> Node:
    return cast(Node, json.loads((ASSETS / "context.json").read_bytes())["@context"])


def context() -> Node:
    return deepcopy(_context())


@cache
def _shapes() -> Graph:
    return Graph().parse(ASSETS / "shapes.ttl", format="turtle")


def iri(kind: str, *components: str | int) -> str:
    path = "/".join(quote(str(item), safe="") for item in components)
    return f"urn:soc:{kind}:{path}"


def _agent(name: str) -> str:
    return iri("agent", name)


def version_iri(commitment_id: str, version: int) -> str:
    return iri("version", commitment_id, version)


def version_nodes(
    identity: Commitment,
    version: CommitmentVersion,
    previous: str | None = None,
) -> list[Node]:
    commitment: Node = {
        "@id": iri("commitment", identity.id),
        "@type": "Commitment",
        "operation": iri("operation", identity.operation),
        "owner": _agent(identity.owner),
        "debtor": _agent(identity.debtor),
        "creditor": _agent(identity.creditor),
    }
    record: Node = {
        "@id": version_iri(identity.id, version.version),
        "@type": "CommitmentVersion",
        "commitment": commitment["@id"],
        "version": version.version,
        "scope": list(version.scope),
        "rendering": version.rendering,
        "rationale": version.rationale,
        "evidence": list(version.evidence),
        "proposer": _agent(version.proposer),
        "ratifier": _agent(version.ratifier),
        "consistency": version.consistency,
        "state": version.state,
        "strength": version.strength,
        "validFrom": version.valid_from,
    }
    if version.valid_until is not None:
        record["validUntil"] = version.valid_until
    if version.source_quote is not None:
        record["sourceQuote"] = {
            "sentenceId": version.source_quote.sentence_id,
            "text": version.source_quote.text,
        }
    if version.rule is not None:
        record["checkableRule"] = {
            "kind": version.rule.kind,
            "parameters": json.dumps(
                version.rule.parameters, sort_keys=True, allow_nan=False
            ),
        }
    if previous is not None:
        record["wasRevisionOf"] = previous
    return [commitment, record]


def event_nodes(event_id: str, kind: str, seq: int, actor: Actor) -> tuple[Node, Node]:
    event: Node = {
        "@id": iri("event", event_id),
        "@type": ["prov:Activity", "LedgerEvent"],
        "sequence": seq,
        "eventType": kind,
        "actor": _agent(actor.agent),
    }
    agent: Node = {"@id": _agent(actor.agent), "role": actor.role}
    if kind == "BriefChanged":
        event["@type"] = ["prov:Activity", "LedgerEvent", "BriefChange"]
    return event, agent


def epoch_nodes(
    run_id: str,
    actor: Actor,
    rebound: ExecutorRebound,
    seq: int,
    previous_start: int = 0,
) -> list[Node]:
    nodes: list[Node] = []
    for model, start, end in (
        (rebound.old_model, previous_start, seq),
        (rebound.new_model, seq, None),
    ):
        executor = iri("executor", run_id, model)
        nodes.append(
            {
                "@id": executor,
                "@type": "prov:SoftwareAgent",
                "actedOnBehalfOf": _agent(actor.agent),
            }
        )
        epoch: Node = {
            "@id": iri("epoch", run_id, start),
            "@type": "ExecutorEpoch",
            "executor": executor,
            "validFrom": start,
        }
        if end is not None:
            epoch["validUntil"] = end
        nodes.append(epoch)
    return nodes


def validate_records(nodes: list[Node]) -> int:
    """Validate JSON-LD without fetching contexts and return validation nanoseconds."""
    started = perf_counter_ns()
    document = {"@context": context(), "@graph": nodes}
    graph = Graph().parse(data=json.dumps(document, allow_nan=False), format="json-ld")
    conforms, report, _ = validate(
        graph, shacl_graph=_shapes(), inference="none", inplace=True
    )
    elapsed = perf_counter_ns() - started
    if not conforms:
        names: set[str] = set()
        for source in report.objects(None, SH.sourceShape):
            parents = list(report_shape_parents(source))
            names.update(
                str(node).removeprefix(NAMESPACE) for node in parents or [source]
            )
        raise ShapeError(", ".join(sorted(names)))
    return elapsed


def report_shape_parents(source: object) -> list[object]:
    # pySHACL reports the property shape for direct property violations.
    return list(_shapes().subjects(SH.property, source))  # type: ignore[arg-type]

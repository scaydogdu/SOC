import json

import pytest
from rdflib import Graph

from soc.ledger.schema.validation import (
    ASSETS,
    NAMESPACE,
    ShapeError,
    context,
    validate_records,
)

FIXTURES = ASSETS / "fixtures"


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("valid-*.json")), ids=lambda path: path.stem
)
def test_valid_graphs(fixture):
    assert validate_records(json.loads(fixture.read_bytes())) > 0


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("invalid-*.json")), ids=lambda path: path.stem
)
def test_invalid_graphs_fail_only_the_named_shape(fixture):
    shape = fixture.stem.split("-")[1]
    with pytest.raises(ShapeError, match=f"^{shape}$"):
        validate_records(json.loads(fixture.read_bytes()))


def test_ontology_matches_context_and_rebind_preserves_intervals():
    graph = Graph().parse(ASSETS / "ontology.ttl", format="turtle")
    assert str(graph.namespace_manager.store.namespace("soc")) == NAMESPACE
    assert context()["@vocab"] == NAMESPACE
    nodes = json.loads((FIXTURES / "valid-rebound.json").read_bytes())
    without_rebound = [
        node
        for node in nodes
        if node.get("@type") in ("Commitment", "CommitmentVersion")
    ]
    assert validate_records(without_rebound) > 0
    assert [
        node["validFrom"] for node in nodes if node.get("@type") == "CommitmentVersion"
    ] == [1]
    assert all("validUntil" not in node for node in without_rebound)


@pytest.mark.parametrize("strengths", [[], ["must", "may"]])
def test_strength_shape_requires_exactly_one_value(strengths):
    nodes = json.loads((FIXTURES / "valid-strength.json").read_bytes())
    nodes[1]["strength"] = strengths
    with pytest.raises(ShapeError, match="^StrengthShape$"):
        validate_records(nodes)

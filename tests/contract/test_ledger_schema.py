import json

import pytest
from rdflib import BNode, Graph, Literal
from rdflib.namespace import OWL, RDF, RDFS, SH

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


@pytest.mark.parametrize(
    ("field", "value"),
    [("antecedent", ["different scope"]), ("consequent", "different rendering")],
)
def test_commitment_roles_match_the_ruling(field, value):
    nodes = json.loads((FIXTURES / "valid-strength.json").read_bytes())
    nodes[1][field] = value
    with pytest.raises(ShapeError, match="^CommitmentRolesShape$"):
        validate_records(nodes)


def test_ontology_declares_ledger_and_shape_vocabulary():
    ontology = Graph().parse(ASSETS / "ontology.ttl", format="turtle")
    shapes = Graph().parse(ASSETS / "shapes.ttl", format="turtle")
    fixture_graph = Graph()
    for fixture in FIXTURES.glob("valid-*.json"):
        fixture_graph.parse(
            data=json.dumps(
                {"@context": context(), "@graph": json.loads(fixture.read_bytes())}
            ),
            format="json-ld",
        )

    classes = (
        set(fixture_graph.objects(None, RDF.type))
        | set(shapes.objects(None, SH.targetClass))
        | set(shapes.objects(None, SH["class"]))
    )
    for term in classes:
        if str(term).startswith(NAMESPACE):
            assert (term, RDF.type, OWL.Class) in ontology, term

    for _, predicate, value in fixture_graph:
        if str(predicate).startswith(NAMESPACE):
            declaration = (
                OWL.DatatypeProperty
                if isinstance(value, Literal)
                else OWL.ObjectProperty
            )
            assert (predicate, RDF.type, declaration) in ontology, predicate

    properties = set(shapes.objects(None, SH.equals)) | set(
        shapes.objects(None, SH.lessThan)
    )
    for path in shapes.objects(None, SH.path):
        properties.update(shapes.items(path) if isinstance(path, BNode) else [path])
    for term in properties:
        if str(term).startswith(NAMESPACE):
            assert any(
                (term, RDF.type, declaration) in ontology
                for declaration in (OWL.DatatypeProperty, OWL.ObjectProperty)
            ), term

    for declaration in (OWL.Class, OWL.DatatypeProperty, OWL.ObjectProperty):
        for term in ontology.subjects(RDF.type, declaration):
            if str(term).startswith(NAMESPACE):
                assert ontology.value(term, RDFS.label), term
                assert ontology.value(term, RDFS.comment), term

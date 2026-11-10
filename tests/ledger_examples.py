from soc.ledger.models import Actor, Commitment, Quote, Rule, Ruling

OWNER = Actor(agent="owner", model="model-1", role="principal")
OTHER = Actor(agent="other", model="model-2", role="executor")
IDENTITY = Commitment(
    id="D1", operation="toy", owner="owner", debtor="client", creditor="reader"
)
RULING = Ruling(
    scope=["access token"],
    rendering="erişim jetonu",
    source_quote=Quote(sentence_id="9999-1-s01", text="access token"),
    rule=Rule(kind="term", parameters={"term": "access token"}),
    rationale="Client terminology",
    evidence=["source:9999-1-s01", "brief:B1"],
    proposer="translator",
    ratifier="owner",
    consistency="ONE",
    state="ratified",
)

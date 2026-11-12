"""Shared HTTP transport and structured-schema handling for provider adapters."""

import os
from copy import deepcopy
from typing import TypeVar

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from soc.providers.models import Outcome, ProviderRequest, ProviderRequestError, Reply

type JsonObject = dict[str, JsonValue]
WireResponse = TypeVar("WireResponse", bound=BaseModel)


def api_key(*names: str) -> str:
    for name in names:
        if value := os.environ.get(name):
            return value
    raise ProviderRequestError(f"set {' or '.join(names)} in the environment")


async def post[WireResponse: BaseModel](
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: JsonObject,
    response_type: type[WireResponse],
) -> WireResponse | Reply:
    try:
        response = await client.post(
            url, headers=headers, json=payload, follow_redirects=False
        )
    except httpx.TransportError:
        return Reply(outcome=Outcome.SERVER_ERROR)
    status = response.status_code
    if status == 429:
        return Reply(outcome=Outcome.RATE_LIMITED)
    if status == 408 or status >= 500:
        return Reply(outcome=Outcome.SERVER_ERROR)
    if not response.is_success:
        raise ProviderRequestError(f"provider rejected the request (HTTP {status})")
    try:
        return response_type.model_validate_json(response.content)
    except ValidationError:
        return Reply(outcome=Outcome.PARSE_FAILED)


def sampling(request: ProviderRequest, allowed: set[str]) -> JsonObject:
    if request.sampling.keys() - allowed:
        raise ProviderRequestError("unsupported sampling settings for this provider")
    return request.sampling


def closed_schema(schema: JsonObject, *, require_all: bool) -> JsonObject:
    """Convert Pydantic object nodes to the providers' closed-object subset."""
    result = deepcopy(schema)
    if result.get("type") != "object":
        raise ProviderRequestError(
            "the structured output schema must have an object root"
        )
    _close_node(result, require_all=require_all)
    return result


def _close_node(schema: JsonObject, *, require_all: bool) -> None:
    schema.pop("default", None)
    properties = schema.get("properties")
    if schema.get("type") == "object":
        if schema.get("additionalProperties") not in (None, False):
            raise ProviderRequestError(
                "use declared fields instead of open object mappings"
            )
        schema["additionalProperties"] = False
        if require_all and isinstance(properties, dict):
            schema["required"] = list(properties)

    for name in ("properties", "$defs"):
        entries = schema.get(name)
        if isinstance(entries, dict):
            for item in entries.values():
                if isinstance(item, dict):
                    _close_node(item, require_all=require_all)
    items = schema.get("items")
    if isinstance(items, dict):
        _close_node(items, require_all=require_all)
    for name in ("anyOf", "oneOf", "allOf", "prefixItems"):
        entries = schema.get(name)
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    _close_node(item, require_all=require_all)

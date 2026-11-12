"""Run the offline Inspect AI experiment documented in M1_STATUS.md."""

import asyncio
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inspect_ai.model import (
    CachePolicy,
    GenerateConfig,
    ModelOutput,
    ModelUsage,
    ResponseSchema,
    get_model,
)
from inspect_ai.util import json_schema
from pydantic import BaseModel


class Answer(BaseModel):
    value: str


class UnexpectedCall(RuntimeError):
    pass


def unavailable(*args, **kwargs):
    raise UnexpectedCall("transport or provider call")


async def scripted(*, input, config, **kwargs):
    assert config.reasoning_effort == "low"
    assert config.response_schema.name == "answer"
    value = input[-1].text.split()[-1]
    result = ModelOutput.from_content(
        "mockllm/soc-trial", Answer(value=value).model_dump_json()
    )
    result.usage = ModelUsage(input_tokens=11, output_tokens=5, total_tokens=16)
    return result


async def trial():
    settings = GenerateConfig(
        reasoning_effort="low",
        response_schema=ResponseSchema(
            name="answer", json_schema=json_schema(Answer), strict=True
        ),
    )
    cache = CachePolicy(expiry=None, per_epoch=False, scopes={"replicate": "0"})
    prompts = ["Return alpha", "Return beta"]
    async with get_model("mockllm/soc-trial") as model:
        with patch.object(model.api, "generate", side_effect=scripted):
            recorded = [
                await model.generate(prompt, config=settings, cache=cache)
                for prompt in prompts
            ]
    async with get_model("mockllm/soc-trial") as model:
        with patch.object(model.api, "generate", side_effect=unavailable) as provider:
            replayed = [
                await model.generate(prompt, config=settings, cache=cache)
                for prompt in prompts
            ]
            assert [item.completion.encode() for item in recorded] == [
                item.completion.encode() for item in replayed
            ]
            assert [item.usage for item in recorded] == [
                item.usage for item in replayed
            ]
            assert [
                Answer.model_validate_json(item.completion).value for item in replayed
            ] == ["alpha", "beta"]
            provider.assert_not_called()
            print(
                "2 calls: response bytes and reported usage matched with provider and sockets disabled"
            )
            try:
                await model.generate("Return gamma", config=settings, cache=cache)
            except UnexpectedCall:
                provider.assert_called_once()
                print("Cache miss attempted provider generation")
            else:
                raise AssertionError("Cache miss did not attempt generation")


if __name__ == "__main__":
    with (
        TemporaryDirectory() as cache_dir,
        patch.dict(os.environ, {"INSPECT_CACHE_DIR": cache_dir}),
        patch("socket.socket.connect", unavailable),
        patch("socket.socket.connect_ex", unavailable),
        patch("socket.getaddrinfo", unavailable),
    ):
        asyncio.run(trial())

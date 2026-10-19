"""The Claude client Karo talks to the model through, and a scripted stand-in for tests.

Both return objects shaped like the SDK's message: `.content` blocks and a `.stop_reason`. The loop treats them alike.
"""

from __future__ import annotations

import copy
import itertools
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import anthropic
from anthropic.types.beta import BetaTextBlock, BetaThinkingBlock, BetaToolUseBlock

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
# Opts in to the server re-running a refused request on Anthropic's recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Claude(Protocol):
    def create(self, *, system: str, messages: list[dict], tools: list[dict], effort: str): ...


class ClaudeClient:
    def __init__(self, api_key: str, model: str = MODEL, *, http_client: anthropic.DefaultHttpxClient | None = None):
        self._model = model
        self._sdk = anthropic.Anthropic(api_key=api_key, http_client=http_client)

    def create(self, *, system: str, messages: list[dict], tools: list[dict], effort: str):
        # Neither `thinking` (adaptive is always on) nor a forced tool_choice may be sent: this model returns 400.
        return self._sdk.beta.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto"},
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )


# --- the stand-in for tests ----------------------------------------------------------------------


@dataclass
class FakeResponse:
    content: list
    stop_reason: str
    stop_details: object | None = None

    def model_dump(self, **options) -> dict:
        return {
            "role": "assistant",
            "content": [block.model_dump(**options) for block in self.content],
            "stop_reason": self.stop_reason,
        }

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


Step = FakeResponse | Callable[[dict], FakeResponse]

_call_ids = itertools.count(1)


def text_response(text: str, *, thinking: str | None = None) -> FakeResponse:
    blocks = [] if thinking is None else [BetaThinkingBlock(type="thinking", thinking=thinking, signature="sig")]
    return FakeResponse(blocks + [BetaTextBlock(type="text", text=text)], "end_turn")


def tool_response(
    *calls: tuple[str, dict],
    text: str | None = None,
    thinking: str | None = None,
    signature: str = "sig",
    stop_reason: str = "tool_use",
) -> FakeResponse:
    blocks: list = (
        [] if thinking is None else [BetaThinkingBlock(type="thinking", thinking=thinking, signature=signature)]
    )
    if text is not None:
        blocks.append(BetaTextBlock(type="text", text=text))
    for name, tool_input in calls:
        blocks.append(
            BetaToolUseBlock(type="tool_use", id=f"toolu_fake_{next(_call_ids)}", name=name, input=tool_input)
        )
    return FakeResponse(blocks, stop_reason)


def refusal_response(*partial_content) -> FakeResponse:
    return FakeResponse(list(partial_content), "refusal")


class FakeClaude:
    """Answers with a scripted list of responses, in order, and keeps a copy of every request it was sent.

    A step may be a function of the request, for answers that depend on what the AI was just told (a fee id).
    """

    def __init__(self, steps: Iterable[Step]) -> None:
        self._steps = deque(steps)
        self.requests: list[dict] = []

    def create(self, *, system: str, messages: list[dict], tools: list[dict], effort: str):
        # A copy, because the loop keeps appending to the messages list it passes in.
        request = copy.deepcopy({"system": system, "messages": messages, "tools": tools, "effort": effort})
        self.requests.append(request)
        if not self._steps:
            raise AssertionError("FakeClaude was called more times than it has scripted responses.")
        step = self._steps.popleft()
        return step(request) if callable(step) else step

"""The brain: conversation, memory, and tool-calling.

Two ways to answer:

* `respond()` — the real path. The model is given the tool schema and decides for itself
  which actions to take, in a loop, until it has something to say. This is what makes
  "open the folder my CVs are in" work without a regex.
* `stream_reply()` — plain text streaming for providers that will not do tools (Gemini
  fallback), so the assistant never loses the ability to talk.

History is kept in OpenAI message shape because that is what the tool-calling providers
use; it is converted on the way to Gemini.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Iterator

from ..ai import DeepSeek, Gemini, Groq, extract_text, function_calls
from ..config import CONFIG
from ..memory import Memory
from ..tools import Tools
from .persona import system_prompt

log = logging.getLogger("brain")

MAX_TOOL_ROUNDS = 4


class Brain:
    def __init__(self, gemini: Gemini | None = None, deepseek: DeepSeek | None = None,
                 groq: Groq | None = None,
                 chain: tuple[tuple[str, str], ...] | None = None,
                 system: str | None = None, max_turns: int | None = None,
                 tools: Tools | None = None, memory: Memory | None = None) -> None:
        self.gemini = gemini or Gemini()
        if deepseek is None and CONFIG.deepseek_api_key:
            deepseek = DeepSeek()
        self.deepseek = deepseek
        if groq is None and CONFIG.groq_api_key:
            groq = Groq()
        self.groq = groq
        self.chain = chain or CONFIG.llm_chain
        self.override_system = system
        self.max_turns = max_turns or CONFIG.max_history_turns
        self.tools = tools if tools is not None else Tools()
        self.memory = memory if memory is not None else Memory()
        self.messages: list[dict] = []
        self.last_reply = ""
        self.last_model = self.chain[0][1] if self.chain else ""
        # Per-model circuit breaker: after two consecutive failures a model is skipped
        # for a minute, so one dead endpoint cannot tax every turn.
        self._fail_streak: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    # ------------------------------------------------------------------ history
    def reset(self) -> None:
        self.messages.clear()

    def _trim(self) -> None:
        keep = self.max_turns * 2
        if len(self.messages) > keep:
            self.messages = self.messages[-keep:]

    def _system(self) -> str:
        return self.override_system or system_prompt(CONFIG.user_title, self.memory.context())

    def remember_user_fact(self, fact: str) -> None:
        self.memory.remember(fact)

    # ---------------------------------------------------------------- providers
    def _tool_providers(self) -> list[tuple[str, object, str]]:
        """Providers that support OpenAI-style tool calling, in configured order."""
        providers: list[tuple[str, object, str]] = []
        for provider, model in self.chain:
            if provider == "groq" and self.groq is not None:
                providers.append((provider, self.groq, model))
            elif provider == "deepseek" and self.deepseek is not None:
                providers.append((provider, self.deepseek, model))
        return providers

    def _text_providers(self) -> list[tuple[str, str]]:
        chain: list[tuple[str, str]] = []
        for provider, model in self.chain:
            if provider == "gemini" and CONFIG.gemini_api_key:
                chain.append((provider, model))
        return chain or ([("gemini", CONFIG.chat_model)] if CONFIG.gemini_api_key else [])

    # ------------------------------------------------------------------ the turn
    def respond(self, user_text: str) -> tuple[str, list[str]]:
        """Answer `user_text`, calling tools as needed. Returns (spoken_reply, actions)."""
        self.messages.append({"role": "user", "content": user_text})
        actions: list[str] = []
        reply = ""
        try:
            reply = self._answer(actions)
        except Exception as exc:  # noqa: BLE001 - report, never crash the turn
            log.error("brain failed: %s", exc)
            raise
        finally:
            if reply:
                self.messages.append({"role": "assistant", "content": reply})
                self.last_reply = reply
                self._trim()
                try:
                    self.memory.log_turn(user_text, reply)
                except Exception:  # noqa: BLE001 - memory must not break answering
                    log.debug("memory log failed", exc_info=True)
        return reply.strip(), actions

    def _answer(self, actions: list[str]) -> str:
        """Answer with the best available brain.

        Tool-capable OpenAI-style providers are tried first; Gemini now follows with its
        native function calling instead of being a text-only last resort. That distinction
        matters: with DeepSeek unfunded and no Groq key, Gemini is the only brain here, and
        routing it around the toolbelt is why Jarvis described actions it never took.
        """
        last_error: Exception | None = None
        for provider, client, model in self._tool_providers():
            if self._cooling(model):
                continue
            try:
                self.last_model = model
                reply = self._tool_loop(client, model, actions)
                self._note_success(model)
                return reply
            except Exception as exc:  # noqa: BLE001 - try the next provider
                last_error = exc
                self._note_failure(model)
                log.warning("%s/%s tool turn failed (%s); trying next", provider, model, exc)
        try:
            return self._gemini_tool_loop(actions)
        except Exception as exc:  # noqa: BLE001
            raise last_error or exc

    def _tool_loop(self, client, model: str, actions: list[str]) -> str:  # noqa: ANN001
        messages: list[dict] = [{"role": "system", "content": self._system()}]
        messages.extend(self.messages[-self.max_turns * 2:])
        schemas = self.tools.schemas()
        for round_index in range(MAX_TOOL_ROUNDS):
            data = client.chat(messages, tools=schemas, temperature=0.6,
                               max_output_tokens=500, timeout=CONFIG.llm_timeout)
            choices = data.get("choices") or []
            if not choices:
                return "I didn't get an answer from my language model, sir."
            message = choices[0].get("message", {})
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip()

            log.info("tool round %d/%d: model requested %d call(s)",
                     round_index + 1, MAX_TOOL_ROUNDS, len(calls))
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            })
            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                raw = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except json.JSONDecodeError:
                    arguments = {}
                result = self.tools.execute(name, arguments)
                actions.append(f"{name}: {result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call_{round_index}"),
                    "content": result,
                })

        # Ran out of tool rounds: ask for a one-line summary without further tools.
        try:
            data = client.chat(messages, tools=None, temperature=0.4, max_output_tokens=160)
            return (data["choices"][0]["message"].get("content") or "Done, sir.").strip()
        except Exception:  # noqa: BLE001
            return "Done, sir."

    # ------------------------------------------------------- gemini native tools
    def _gemini_declarations(self) -> list[dict]:
        """Translate the OpenAI-style tool schemas into Gemini function declarations."""
        declarations: list[dict] = []
        for schema in self.tools.schemas():
            function = schema.get("function", schema)
            declarations.append({
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get("parameters")
                or {"type": "object", "properties": {}},
            })
        return declarations

    def _gemini_contents(self) -> list[dict]:
        """Conversation history in Gemini's shape, without OpenAI tool plumbing."""
        contents: list[dict] = []
        for message in self.messages:
            role = message.get("role")
            if role == "tool":
                continue
            if role == "assistant" and message.get("tool_calls") and not message.get("content"):
                continue
            text = message.get("content")
            if not text:
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": text}],
            })
        return contents

    def _gemini_tool_loop(self, actions: list[str]) -> str:
        """The real turn for a Gemini brain: model -> tool -> model, until it can speak."""
        contents = self._gemini_contents()
        declarations = self._gemini_declarations()
        models = [model for _provider, model in self._text_providers() if not self._cooling(model)]
        if not models:   # everything is in cooldown: one attempt is better than silence
            models = [model for _provider, model in self._text_providers()]
        last_error: Exception | None = None
        for model in models:
            try:
                self.last_model = model
                for round_index in range(MAX_TOOL_ROUNDS):
                    data = self.gemini.generate(
                        model, contents, system=self._system(), tools=declarations,
                        temperature=0.6, max_output_tokens=500, timeout=CONFIG.llm_timeout,
                    )
                    calls = function_calls(data)
                    if not calls:
                        self._note_success(model)
                        return extract_text(data).strip()
                    log.info("gemini tool round %d/%d: model requested %d call(s)",
                             round_index + 1, MAX_TOOL_ROUNDS, len(calls))
                    contents.append({
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": call.get("name", ""),
                                              "args": call.get("args") or {}}}
                            for call in calls
                        ],
                    })
                    responses = []
                    for call in calls:
                        name = call.get("name", "")
                        arguments = call.get("args") or {}
                        result = self.tools.execute(name, arguments)
                        actions.append(f"{name}: {result}")
                        responses.append({
                            "functionResponse": {
                                "name": name,
                                "response": {"result": result},
                            },
                        })
                    contents.append({"role": "user", "parts": responses})
                # Ran out of rounds: ask for a one-line summary with no further tools.
                data = self.gemini.generate(
                    model, contents, system=self._system(), tools=None,
                    temperature=0.4, max_output_tokens=160, timeout=CONFIG.llm_timeout,
                )
                self._note_success(model)
                return extract_text(data).strip() or "Done, sir."
            except Exception as exc:  # noqa: BLE001 - rate limit, DNS, SSL: next model
                last_error = exc
                self._note_failure(model)
                log.warning("gemini/%s failed (%s); trying next", model, exc)
        raise last_error or RuntimeError("no brain available")

    # ---------------------------------------------------------------- circuit breaker
    def _cooling(self, model: str) -> bool:
        return time.monotonic() < self._cooldown_until.get(model, 0.0)

    def _note_failure(self, model: str) -> None:
        streak = self._fail_streak.get(model, 0) + 1
        self._fail_streak[model] = streak
        if streak >= 2:
            self._cooldown_until[model] = time.monotonic() + CONFIG.llm_model_cooldown_s
            self._fail_streak[model] = 0
            log.warning("brain model %s cooling down for %.0f s",
                        model, CONFIG.llm_model_cooldown_s)

    def _note_success(self, model: str) -> None:
        self._fail_streak.pop(model, None)
        self._cooldown_until.pop(model, None)

    # ------------------------------------------------------------ text-only fallback
    def _gemini_stream(self) -> Iterator[str]:
        # Only include clean user/assistant turns. Tool-call messages are in OpenAI
        # format (role=tool, tool_calls=[...]) which Gemini's API cannot parse.
        # Filtering them keeps the conversation readable when we fall back from Groq.
        contents: list[dict] = []
        for message in self.messages:
            role = message.get("role")
            # Skip tool result turns and assistant messages that only contain tool calls
            if role == "tool":
                continue
            if role == "assistant" and message.get("tool_calls") and not message.get("content"):
                continue
            text = message.get("content")
            if not text:
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": text}],
            })
        system = self._system()
        last_error: Exception | None = None
        for provider, model in self._text_providers():
            if self._cooling(model):
                continue
            try:
                self.last_model = model
                yield from self.gemini.stream_text(
                    model, contents, system=system, temperature=0.75,
                    max_output_tokens=400, timeout=CONFIG.llm_timeout,
                )
                self._note_success(model)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._note_failure(model)
                log.warning("gemini/%s failed (%s); trying next", model, exc)
        if last_error is not None:
            raise last_error

    def reply(self, user_text: str) -> str:
        """Text-only answer, no tools (kept for simple callers and diagnostics)."""
        self.messages.append({"role": "user", "content": user_text})
        text = "".join(self._gemini_stream()).strip()
        self.messages.append({"role": "assistant", "content": text})
        self._trim()
        return text

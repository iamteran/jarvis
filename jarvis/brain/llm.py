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
from typing import Iterator

from ..ai import DeepSeek, Gemini, Groq
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
            providers = self._tool_providers()
            if providers:
                reply = self._tool_turn(providers, actions)
            else:
                reply = "".join(self._gemini_stream())
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

    def _tool_turn(self, providers, actions: list[str]) -> str:  # noqa: ANN001
        last_error: Exception | None = None
        for provider, client, model in providers:
            try:
                self.last_model = model
                return self._tool_loop(client, model, actions)
            except Exception as exc:  # noqa: BLE001 - try the next provider
                last_error = exc
                log.warning("%s/%s tool turn failed (%s); trying next", provider, model, exc)
        raise last_error or RuntimeError("no tool-capable provider")

    def _tool_loop(self, client, model: str, actions: list[str]) -> str:  # noqa: ANN001
        messages: list[dict] = [{"role": "system", "content": self._system()}]
        messages.extend(self.messages[-self.max_turns * 2:])
        schemas = self.tools.schemas()
        for round_index in range(MAX_TOOL_ROUNDS):
            data = client.chat(messages, tools=schemas, temperature=0.6,
                               max_output_tokens=500, timeout=90)
            choices = data.get("choices") or []
            if not choices:
                return "I didn't get an answer from my language model, sir."
            message = choices[0].get("message", {})
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip()

            log.info("model requested %d tool call(s)", len(calls))
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

    # ------------------------------------------------------------ text-only fallback
    def _gemini_stream(self) -> Iterator[str]:
        contents: list[dict] = []
        for message in self.messages:
            role = message.get("role")
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
            try:
                self.last_model = model
                yield from self.gemini.stream_text(
                    model, contents, system=system, temperature=0.75, max_output_tokens=400,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
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

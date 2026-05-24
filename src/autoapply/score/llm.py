"""Native LLM clients — no LiteLLM.

- Gemini (scorer + agent): google-genai SDK
- Groq  (composer):        groq SDK
- Sarvam:                  HTTPS chat-completions API

Model names in .env use LiteLLM-style prefixes (gemini/..., groq/..., sarvam/...)
which we strip before passing to provider APIs so the config format is consistent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

from autoapply.config import get_settings


def _parse_json_robust(text: str) -> dict[str, Any]:
    """Parse LLM output to a dict, tolerating empty strings and markdown fences."""
    if not text:
        return {}
    # Fast path — clean JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip common markdown wrappers
    s = text.strip()
    for prefix in ("```json", "```"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.endswith("```"):
        s = s[:-3]
    s = s.strip()
    if s:
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    # Last resort: grab the first {...} block
    import re as _re
    m = _re.search(r"\{.*\}", s, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    log.warning("chat_json: could not parse LLM output (len=%d): %.120r", len(text), text)
    return {}


def _strip_prefix(model: str) -> str:
    """'gemini/gemini-2.0-flash' → 'gemini-2.0-flash', 'groq/llama-...' → 'llama-...'"""
    return model.split("/", 1)[-1] if "/" in model else model


def _split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Split OpenAI-style messages into (system_instruction, user_content)."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m["content"])
        else:
            user_parts.append(m["content"])
    return "\n".join(system_parts), "\n".join(user_parts)


@dataclass(slots=True)
class GeminiClient:
    model: str
    api_key: str = ""
    service_account_json: Path | None = None
    google_cloud_location: str = "us-central1"

    def _client(self, genai: Any) -> Any:
        if self.service_account_json:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                str(self.service_account_json),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return genai.Client(
                vertexai=True,
                credentials=credentials,
                project=credentials.project_id,
                location=self.google_cloud_location,
            )

        if self.api_key:
            return genai.Client(api_key=self.api_key)

        return genai.Client()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        from google import genai
        from google.genai import types

        client = self._client(genai)
        system, user = _split_messages(messages)
        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        response = await client.aio.models.generate_content(
            model=_strip_prefix(self.model),
            contents=user,
            config=cfg,
        )
        return response.text or ""

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        text = await self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        return _parse_json_robust(text)


@dataclass(slots=True)
class GroqClient:
    model: str
    api_key: str

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.api_key)
        kwargs: dict[str, Any] = {
            "model": _strip_prefix(self.model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        text = await self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        return _parse_json_robust(text)


@dataclass(slots=True)
class SarvamClient:
    model: str
    api_key: str

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        import httpx

        prompt_messages = messages
        if json_mode:
            prompt_messages = [
                *messages,
                {"role": "user", "content": "Return only valid JSON. Do not wrap it in markdown."},
            ]

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.sarvam.ai/v1/chat/completions",
                headers={
                    "api-subscription-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": _strip_prefix(self.model),
                    "messages": prompt_messages,
                    "temperature": temperature,
                    "max_completion_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"].get("content") or ""

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        text = await self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        return _parse_json_robust(text)


@dataclass(slots=True)
class OpenAIClient:
    """OpenAI-compatible client — works for OpenAI, Ollama, LM Studio, Mistral, etc.

    For Ollama set `base_url="http://localhost:11434/v1"` and `api_key="ollama"`.
    """

    model: str
    api_key: str = "ollama"
    base_url: str = "https://api.openai.com/v1"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        kwargs: dict[str, Any] = {
            "model": _strip_prefix(self.model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        text = await self.chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        return _parse_json_robust(text)


def _secret(s: Any) -> str:
    return s.get_secret_value() if s is not None else ""


AnyLLMClient = GeminiClient | GroqClient | SarvamClient | OpenAIClient


def _make_openai(model: str, api_key: str | None, base_url: str) -> OpenAIClient:
    return OpenAIClient(model=model, api_key=api_key or "ollama", base_url=base_url)


def get_scorer_llm() -> AnyLLMClient:
    s = get_settings()
    provider = s.scorer_provider.lower().strip()
    if provider == "groq":
        return GroqClient(model=s.scorer_model, api_key=_secret(s.scorer_api_key or s.composer_api_key))
    if provider == "sarvam":
        return SarvamClient(model=s.scorer_model, api_key=_secret(s.scorer_api_key or s.sarvam_api_key))
    if provider in ("openai", "ollama"):
        return _make_openai(s.scorer_model, _secret(s.scorer_api_key or s.openai_api_key), s.openai_base_url)
    return GeminiClient(
        model=s.scorer_model,
        api_key=_secret(s.scorer_api_key),
        service_account_json=s.scorer_service_account_json,
        google_cloud_location=s.scorer_google_cloud_location,
    )


def get_composer_llm() -> AnyLLMClient:
    s = get_settings()
    provider = s.composer_provider.lower().strip()
    if provider == "gemini":
        return GeminiClient(
            model=s.composer_model,
            api_key=_secret(s.composer_api_key or s.scorer_api_key),
            service_account_json=s.scorer_service_account_json,
            google_cloud_location=s.scorer_google_cloud_location,
        )
    if provider == "sarvam":
        return SarvamClient(model=s.composer_model, api_key=_secret(s.composer_api_key or s.sarvam_api_key))
    if provider in ("openai", "ollama"):
        return _make_openai(s.composer_model, _secret(s.composer_api_key or s.openai_api_key), s.openai_base_url)
    return GroqClient(model=s.composer_model, api_key=_secret(s.composer_api_key))


def get_agent_llm() -> AnyLLMClient:
    s = get_settings()
    provider = s.agent_provider.lower().strip()
    if provider == "groq":
        return GroqClient(model=s.agent_model, api_key=_secret(s.agent_api_key or s.composer_api_key))
    if provider == "sarvam":
        return SarvamClient(model=s.agent_model, api_key=_secret(s.agent_api_key or s.sarvam_api_key))
    if provider in ("openai", "ollama"):
        return _make_openai(s.agent_model, _secret(s.agent_api_key or s.openai_api_key), s.openai_base_url)
    return GeminiClient(
        model=s.agent_model,
        api_key=_secret(s.agent_api_key),
        service_account_json=s.agent_service_account_json,
        google_cloud_location=s.agent_google_cloud_location,
    )

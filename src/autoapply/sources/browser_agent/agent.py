"""LLM-powered browser job-discovery agent.

The agent gets 4 tools:
  search_jobs(role, location)       — navigate to portal search URL, return text + links
  fetch_page(url)                   — navigate to any URL (for job detail pages)
  scroll_for_more(times)            — scroll current page to lazy-load more results
  save_job(role, company, ...)      — persist a discovered job immediately

One GeminiJobAgent instance per portal per scraping run. The agent runs a
multi-turn function-calling loop, exercising the tools until it types "DONE" or
exhausts max_turns. All saved JobRecords are returned to the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from autoapply.sources.base import JobRecord, upsert_jobs

from .browser import BrowserSession
from .portals import build_search_url, filter_job_links

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SKIP_EMAIL = ("linkedin.com", "noreply", "no-reply", "naukri.com", "foundit.in", "example.com")

_TOOL_DECLARATIONS = [
    {
        "name": "search_jobs",
        "description": (
            "Navigate to the portal's search results for a role+location. "
            "Returns page text (with job titles, companies, snippets) and a list "
            "of job-detail links [{text, href}]. Always start here for each combo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Job role/title, e.g. 'Machine Learning Engineer'",
                },
                "location": {
                    "type": "string",
                    "description": "City or region, e.g. 'Bangalore'",
                },
            },
            "required": ["role", "location"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Navigate to any URL and return its full text content. Use this to "
            "open individual job detail pages to read the full description and find "
            "an HR / recruiter email address."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full https:// URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "scroll_for_more",
        "description": (
            "Scroll the current page down to trigger lazy-loading of additional job cards. "
            "Returns updated page text after scrolling. Call this after search_jobs if the "
            "initial results look truncated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "times": {
                    "type": "integer",
                    "description": "Number of viewport-heights to scroll (1-5, default 3)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "save_job",
        "description": "Save a discovered job to the pipeline. Call once per job found.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Job title"},
                "company": {"type": "string", "description": "Company name"},
                "location": {"type": "string", "description": "Job location"},
                "jd_url": {"type": "string", "description": "Direct URL of the job posting"},
                "jd_text": {
                    "type": "string",
                    "description": "Job description text (up to 2000 chars)",
                },
                "hr_email": {
                    "type": "string",
                    "description": "HR or recruiter email address, if found in the JD or post. Omit if not found.",
                },
            },
            "required": ["role", "company", "jd_url", "jd_text"],
        },
    },
]

_SYSTEM_TMPL = """\
You are a job-discovery agent for the {portal} job portal, searching for ML / AI / data science \
roles in India.

Your workflow for EACH role + location combination given to you:
1. Call search_jobs(role, location) to load search results.
2. Call scroll_for_more() once to reveal any lazy-loaded cards.
3. Identify the job-detail links returned. For each link:
   a. Call fetch_page(url) to read the full job description.
   b. Scan the page text for an HR / recruiter email address.
   c. Call save_job(...) with the extracted data. Include the email if found.
4. After processing all links from that search, move to the next combo.

Important:
- If the page shows a login wall or CAPTCHA, skip and note it.
- Do NOT save duplicate jobs (same URL twice).
- Extract as many real jobs as possible — don't stop early.
- When all combos are done, reply with exactly: DONE
"""


def _strip_model_prefix(model: str) -> str:
    return model.split("/", 1)[-1] if "/" in model else model


def _secret(value: Any) -> str:
    return value.get_secret_value() if value is not None else ""


def build_job_agent(browser: BrowserSession, *, portal: str, settings: Any) -> GeminiJobAgent:
    provider = settings.agent_provider.lower().strip()
    api_key = _secret(settings.agent_api_key)

    if provider == "gemini" and not api_key:
        api_key = _secret(settings.scorer_api_key)
    elif provider == "groq" and not api_key:
        api_key = _secret(settings.composer_api_key)
    elif provider == "sarvam" and not api_key:
        api_key = _secret(settings.sarvam_api_key)

    return GeminiJobAgent(
        browser,
        portal=portal,
        provider=provider,
        api_key=api_key,
        model_name=_strip_model_prefix(settings.agent_model),
        service_account_json=settings.agent_service_account_json,
        google_cloud_location=settings.agent_google_cloud_location,
    )


def _first_email(text: str) -> str | None:
    for m in _EMAIL_RE.findall(text):
        if not any(skip in m.lower() for skip in _SKIP_EMAIL):
            return m
    return None


class GeminiJobAgent:
    """
    Runs a provider function-calling loop over a BrowserSession to discover jobs.

    One instance handles one portal for the duration of one scraping run.
    """

    def __init__(
        self,
        browser: BrowserSession,
        *,
        portal: str,
        api_key: str = "",
        provider: str = "gemini",
        model_name: str = "gemini-2.0-flash",
        service_account_json: Path | None = None,
        google_cloud_location: str = "us-central1",
        max_turns: int = 80,
    ) -> None:
        self.browser = browser
        self.portal = portal
        self._api_key = api_key
        self._provider = provider.lower().strip()
        self._model_name = model_name
        self._service_account_json = service_account_json
        self._google_cloud_location = google_cloud_location
        self._max_turns = max_turns
        self._saved: list[JobRecord] = []
        self._seen_urls: set[str] = set()

    async def run(self, roles: list[str], locations: list[str]) -> list[JobRecord]:
        """Search all role x location combos; return discovered JobRecords."""
        combos = "\n".join(
            f"- {role} in {loc}" for role in roles for loc in locations
        )
        log.info(
            "[%s] agent starting for %d combos via %s",
            self.portal,
            len(roles) * len(locations),
            self._provider,
        )

        try:
            if self._provider == "groq":
                await self._run_groq(combos)
            elif self._provider == "sarvam":
                await self._run_sarvam(combos)
            elif self._provider == "gemini":
                await self._run_gemini(combos)
            else:
                log.error("[%s] unsupported agent provider: %s", self.portal, self._provider)
        except Exception:
            log.exception("[%s] agent error", self.portal)

        log.info("[%s] agent finished, saved %d jobs", self.portal, len(self._saved))
        return self._saved

    async def _run_gemini(self, combos: str) -> None:
        from google import genai
        from google.genai import types

        client = self._gemini_client(genai)
        config = types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=_TOOL_DECLARATIONS)],
            system_instruction=_SYSTEM_TMPL.format(portal=self.portal),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = [
            types.Content(
                role="user",
                parts=[types.Part(text=f"Search these role+location combos:\n{combos}")],
            )
        ]

        response = await client.aio.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=config,
        )
        await self._loop_gemini(client, types, config, contents, response)

    def _gemini_client(self, genai: Any) -> Any:
        if self._service_account_json:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                str(self._service_account_json),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return genai.Client(
                vertexai=True,
                credentials=credentials,
                project=credentials.project_id,
                location=self._google_cloud_location,
            )

        if self._api_key:
            return genai.Client(api_key=self._api_key)

        return genai.Client()

    # ------------------------------------------------------------------

    async def _loop_gemini(
        self,
        client: Any,
        types: Any,
        config: Any,
        contents: list[Any],
        response: Any,
    ) -> None:
        for turn in range(self._max_turns):
            fn_calls = response.function_calls or []

            if not fn_calls:
                text = response.text or ""
                log.info("[%s] agent text reply (turn %d): %s", self.portal, turn, text[:200])
                break

            fn_parts: list[Any] = []
            for fc in fn_calls:
                result = await self._dispatch(fc.name, dict(fc.args))
                kwargs = {"name": fc.name, "response": {"result": result}}
                if getattr(fc, "id", None):
                    kwargs["id"] = fc.id
                fn_parts.append(
                    types.Part.from_function_response(**kwargs)
                )
                await asyncio.sleep(0.3)  # avoid hammering the API

            contents.append(response.candidates[0].content)
            contents.append(types.Content(role="user", parts=fn_parts))
            response = await client.aio.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )

    async def _run_groq(self, combos: str) -> None:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self._api_key)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_TMPL.format(portal=self.portal)},
            {"role": "user", "content": f"Search these role+location combos:\n{combos}"},
        ]
        tools = [
            {"type": "function", "function": declaration}
            for declaration in _TOOL_DECLARATIONS
        ]

        for turn in range(self._max_turns):
            response = await client.chat.completions.create(
                model=self._model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=2048,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            if not tool_calls:
                text = message.content or ""
                log.info("[%s] agent text reply (turn %d): %s", self.portal, turn, text[:200])
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._dispatch(name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": name,
                        "content": result,
                    }
                )
                await asyncio.sleep(0.3)

    async def _run_sarvam(self, combos: str) -> None:
        import httpx

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_TMPL.format(portal=self.portal)},
            {"role": "user", "content": f"Search these role+location combos:\n{combos}"},
        ]
        tools = [
            {"type": "function", "function": declaration}
            for declaration in _TOOL_DECLARATIONS
        ]
        headers = {
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=90) as client:
            for turn in range(self._max_turns):
                response = await client.post(
                    "https://api.sarvam.ai/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": self._model_name,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                        "max_completion_tokens": 2048,
                    },
                )
                response.raise_for_status()
                data = response.json()
                message = data["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []

                if not tool_calls:
                    text = message.get("content") or ""
                    log.info("[%s] agent text reply (turn %d): %s", self.portal, turn, text[:200])
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )

                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name", "")
                    arguments = function.get("arguments") or "{}"
                    try:
                        args = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except json.JSONDecodeError:
                        args = {}
                    result = await self._dispatch(name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": name,
                            "content": result,
                        }
                    )
                    await asyncio.sleep(0.3)

    async def _dispatch(self, name: str, args: dict) -> str:
        match name:
            case "search_jobs":
                return await self._tool_search(args.get("role", ""), args.get("location", ""))
            case "fetch_page":
                return await self._tool_fetch(args.get("url", ""))
            case "scroll_for_more":
                return await self._tool_scroll(int(args.get("times", 3)))
            case "save_job":
                return await self._tool_save(args)
            case _:
                return f"unknown tool: {name}"

    async def _tool_search(self, role: str, location: str) -> str:
        url = build_search_url(self.portal, role, location)
        log.info("[%s] search_jobs → %s", self.portal, url)
        text = await self.browser.navigate(url, delay=3.0)
        links = await self.browser.get_links()
        job_links = filter_job_links(self.portal, links)

        return (
            f"Portal: {self.portal}\n"
            f"Search URL: {url}\n"
            f"Current URL: {self.browser.url}\n\n"
            f"=== PAGE TEXT (first 4000 chars) ===\n{text[:4000]}\n\n"
            f"=== JOB DETAIL LINKS ({len(job_links)} found) ===\n"
            + "\n".join(f"  {link['text'][:100]} → {link['href']}" for link in job_links)
        )

    async def _tool_fetch(self, url: str) -> str:
        if not url.startswith("http"):
            return "error: invalid URL"
        log.info("[%s] fetch_page → %s", self.portal, url)
        text = await self.browser.navigate(url, delay=2.0)
        email = _first_email(text)

        # Also check mailto: links on the page
        try:
            mailto_links = await self.browser.page.evaluate(
                """() => [...document.querySelectorAll('a[href^="mailto:"]')]
                    .map(a => a.href.replace('mailto:', ''))"""
            )
        except Exception:
            mailto_links = []

        email = email or (mailto_links[0] if mailto_links else None)

        return (
            f"URL: {url}\n"
            f"Email found: {email or '(none)'}\n"
            f"Mailto links: {mailto_links[:3]}\n\n"
            f"=== PAGE TEXT (first 4500 chars) ===\n{text[:4500]}"
        )

    async def _tool_scroll(self, times: int) -> str:
        t = max(1, min(times, 5))
        text = await self.browser.scroll_load(times=t)
        links = await self.browser.get_links()
        job_links = filter_job_links(self.portal, links)
        return (
            f"Scrolled {t}x. New job links found: {len(job_links)}\n"
            f"=== UPDATED TEXT (first 3000 chars) ===\n{text[:3000]}\n\n"
            f"=== LINKS ===\n"
            + "\n".join(f"  {link['text'][:100]} → {link['href']}" for link in job_links)
        )

    async def _tool_save(self, args: dict) -> str:
        role = (args.get("role") or "").strip()
        company = (args.get("company") or "Unknown").strip()
        location = (args.get("location") or "").strip() or None
        jd_url = (args.get("jd_url") or "").strip()
        jd_text = (args.get("jd_text") or "").strip()[:2000]
        hr_email = (args.get("hr_email") or "").strip() or None

        if not role or not jd_url:
            return "error: role and jd_url are required"
        if jd_url in self._seen_urls:
            return f"skipped duplicate: {jd_url}"
        self._seen_urls.add(jd_url)

        rec = JobRecord(
            source=f"scrape:{self.portal}",
            source_id=jd_url,
            company=company,
            role=role,
            jd_text=jd_text or f"{role} at {company}",
            jd_url=jd_url,
            location=location,
            hr_email=hr_email,
        )
        self._saved.append(rec)
        inserted, skipped = await upsert_jobs([rec])
        log.info("[%s] saved: %s @ %s (email=%s)", self.portal, role, company, hr_email or "—")
        if inserted:
            return f"saved and persisted: {role} at {company}"
        if skipped:
            return f"saved duplicate already in Mongo: {role} at {company}"
        return f"saved but Mongo persist returned no change: {role} at {company}"

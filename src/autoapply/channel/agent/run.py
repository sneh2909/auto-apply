"""Channel D — natural-language DOM agent backed by `browser-use`.

Entry point: `run_agent(goal: str) -> AgentRun`.

Flow:
  1. Create AgentRun(goal=..., status=running).
  2. Parse intent via composer/agent LLM → {role_query, location, site, count_limit}.
  3. Persist parsed_intent; if ambiguous, mark `awaiting_user` and return.
  4. Discovery pass: agent navigates and collects candidate jobs → upsert into `jobs`.
  5. Scoring pass: run score.scorer.score_unscored_jobs() on the new jobs.
  6. Apply pass: for each surviving job (fit ≥ threshold, within daily cap), route via
     `channel.router.decide()` and queue Application rows. Submit only after review-queue approval.
  7. Update steps_taken / jobs_discovered / jobs_applied / finished_at on each tick.

This file is the skeleton; the real implementation lives behind the optional `[agent]`
extra (`pip install -e ".[agent]"`) which pulls in `browser-use`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from autoapply.db.models import AgentRun, AgentStatus

log = logging.getLogger(__name__)

MAX_STEPS = 80
MAX_SECONDS = 300
MAX_CANDIDATES = 20


async def run_agent(goal: str) -> AgentRun:
    """Kick off a Channel-D run. Returns the AgentRun document (may finish async)."""
    run = AgentRun(goal=goal, status=AgentStatus.running)
    await run.insert()

    try:
        # TODO: import inside the function so the agent extra remains optional.
        # from browser_use import Agent, Browser
        # llm = get_agent_llm()
        # browser = Browser(user_data_dir=get_settings().browser_profile_dir, headless=get_settings().headless_browser)
        # agent = Agent(task=goal, llm=llm, browser=browser, max_steps=MAX_STEPS)
        # async for step in agent.run():
        #     run.steps_taken = step.index
        #     if step.is_discovery:
        #         job = JobRecord(... from step.payload ...)
        #         await upsert_jobs([job])
        #         run.jobs_discovered.append(str(job_id))
        #     await run.save()
        raise NotImplementedError(
            "Channel D agent is a stub. Install the [agent] extra and implement browser-use loop here."
        )
    except NotImplementedError:
        run.status = AgentStatus.failed
        run.last_error = "stub — not implemented"
    except Exception as e:
        log.exception("agent run failed: %s", e)
        run.status = AgentStatus.failed
        run.last_error = str(e)
    finally:
        run.finished_at = datetime.now(UTC)
        await run.save()

    return run

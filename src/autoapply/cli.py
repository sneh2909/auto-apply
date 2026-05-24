"""Tiny CLI: `autoapply <subcommand>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from autoapply.config import get_settings
from autoapply.db import close_db, init_db


def _configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )


async def _cmd_ingest() -> None:
    from autoapply.scheduler import ingest_once

    await init_db()
    try:
        await ingest_once()
    finally:
        await close_db()



async def _cmd_score(limit: int) -> None:
    from autoapply.scheduler import score_once

    await init_db()
    try:
        await score_once(limit=limit)
    finally:
        await close_db()


async def _cmd_route(limit: int) -> None:
    from autoapply.scheduler import route_once

    await init_db()
    try:
        await route_once(limit=limit)
    finally:
        await close_db()


async def _cmd_agent(goal: str) -> None:
    from autoapply.channel.agent.run import run_agent

    await init_db()
    try:
        run = await run_agent(goal)
        print(f"agent run {run.id} → status={run.status}")
    finally:
        await close_db()


def _cmd_review() -> None:
    import uvicorn

    uvicorn.run("autoapply.review.app:app", host="127.0.0.1", port=8765, reload=False)


def _cmd_scheduler() -> None:
    from autoapply.scheduler import main as sched_main

    asyncio.run(sched_main())


def main() -> None:
    _configure_logging()
    p = argparse.ArgumentParser(prog="autoapply")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="run all sources once")

    p_score = sub.add_parser("score", help="score unscored jobs")
    p_score.add_argument("--limit", type=int, default=100)

    p_route = sub.add_parser("route", help="route scored jobs into Application rows")
    p_route.add_argument("--limit", type=int, default=50)

    p_agent = sub.add_parser("agent", help="run Channel D with a natural-language goal")
    p_agent.add_argument("goal")

    sub.add_parser("review", help="serve the review UI on http://127.0.0.1:8765")
    sub.add_parser("scheduler", help="run the periodic scheduler in the foreground")

    args = p.parse_args()

    if args.cmd == "ingest":
        asyncio.run(_cmd_ingest())
    elif args.cmd == "score":
        asyncio.run(_cmd_score(args.limit))
    elif args.cmd == "route":
        asyncio.run(_cmd_route(args.limit))
    elif args.cmd == "agent":
        asyncio.run(_cmd_agent(args.goal))
    elif args.cmd == "review":
        _cmd_review()
    elif args.cmd == "scheduler":
        _cmd_scheduler()
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()

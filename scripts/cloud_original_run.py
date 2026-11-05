"""GitHub Actions entry point: restore IG session, then publish the next original Reel."""
from __future__ import annotations

import asyncio

from scripts import original_reels  # sets cwd / sys.path
from scripts.cloud_autopilot_run import restore_session_from_env
from services import db


async def _prepare() -> None:
    await db.init_db()
    await restore_session_from_env()


if __name__ == "__main__":
    asyncio.run(_prepare())
    original_reels.post_next()

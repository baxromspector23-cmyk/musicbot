import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))
from services import ig_direct

async def get_reels():
    stats = await ig_direct.get_live_profile_stats(limit=15)
    print("MEDIA_COUNT:", stats["media_count"])
    for it in stats["items"]:
        print(f"{it['code']} | {it.get('taken_at')} | {it.get('title')} | {it.get('permalink')}")

if __name__ == "__main__":
    asyncio.run(get_reels())

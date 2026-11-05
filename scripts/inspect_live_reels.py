import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))
from services import ig_direct

async def get_reels():
    stats = await ig_direct.get_live_profile_stats(limit=25)
    print("USERNAME:", stats["username"])
    print("FOLLOWERS:", stats["followers"])
    print("MEDIA_COUNT:", stats["media_count"])
    print("TOTAL_VIEWS:", stats["total_views"])
    print("AVG_VIEWS:", stats["avg_views"])
    for it in stats["items"]:
        print(f"{it['code']} | Views: {it['views']} | Likes: {it['likes']} | Taken: {it.get('taken_at')} | {it.get('permalink')}")

if __name__ == "__main__":
    asyncio.run(get_reels())

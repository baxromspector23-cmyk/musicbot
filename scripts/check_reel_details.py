import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))
from services import ig_direct

async def main():
    settings = await ig_direct.load_direct_settings()
    cl = ig_direct._create_client(settings)
    medias = cl.user_medias_v1(str(cl.user_id), amount=10)
    for m in medias:
        print("---")
        print("PK:", m.pk, "CODE:", m.code, "TAKEN_AT:", m.taken_at)
        print("DURATION:", m.video_duration)
        cap = (m.caption_text or "").strip()
        first_line = cap.split("\n")[0] if cap else "EMPTY"
        print("FIRST LINE:", first_line)

if __name__ == "__main__":
    asyncio.run(main())

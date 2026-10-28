import json
import sqlite3

stats_data = {
    "username": "coder_bahrom",
    "full_name": "Bahrom | Sayt va Bot Yaratish",
    "biography": "💼 Biznesingiz uchun professional Veb-sayt va Telegram botlar\n🚀 Savdoni oshirish va avtomatlashtirish\n📩 Buyurtma uchun Direct ga yozing",
    "followers": 1191,
    "following": 42,
    "media_count": 7,
    "total_views": 789,
    "total_likes": 36,
    "total_comments": 5,
    "avg_views": 113,
    "items": [
        {
            "pk": "4017662665629805378",
            "code": "DfBmuhsNqtC",
            "permalink": "https://www.instagram.com/reel/DfBmuhsNqtC/",
            "views": 34,
            "likes": 2,
            "comments": 0,
            "title": "🍽 Restoran yoki kafengiz uchun menyu tizimi",
            "taken_at": "2026-10-28T04:44:00+00:00"
        },
        {
            "pk": "4017310000000000001",
            "code": "DfAdZJ3NJr2",
            "permalink": "https://www.instagram.com/reel/DfAdZJ3NJr2/",
            "views": 193,
            "likes": 5,
            "comments": 0,
            "title": "⚡ SEM DEMORA Phonk ostida kod yozish jarayoni",
            "taken_at": "2026-10-27T18:05:00+00:00"
        },
        {
            "pk": "4017280000000000002",
            "code": "DfAXKfyNp58",
            "permalink": "https://www.instagram.com/reel/DfAXKfyNp58/",
            "views": 69,
            "likes": 1,
            "comments": 0,
            "title": "🛍 Do'koningiz savdosini oshiruvchi Internet-do'kon",
            "taken_at": "2026-10-27T17:10:00+00:00"
        },
        {
            "pk": "4017270000000000003",
            "code": "DfAWm78tmxj",
            "permalink": "https://www.instagram.com/reel/DfAWm78tmxj/",
            "views": 164,
            "likes": 7,
            "comments": 1,
            "title": "💻 Kod qatorlaridan restoran saytlarigacha",
            "taken_at": "2026-10-27T17:05:00+00:00"
        },
        {
            "pk": "4017000000000000004",
            "code": "De_ZS4FtR6m",
            "permalink": "https://www.instagram.com/reel/De_ZS4FtR6m/",
            "views": 58,
            "likes": 2,
            "comments": 0,
            "title": "Restoran va kompaniyalar uchun veb-sayt",
            "taken_at": "2026-10-27T08:10:00+00:00"
        },
        {
            "pk": "4015000000000000005",
            "code": "De48UPDtFlV",
            "permalink": "https://www.instagram.com/reel/De48UPDtFlV/",
            "views": 107,
            "likes": 11,
            "comments": 2,
            "title": "Veb-sayt dizaynidagi ajoyib animatsiyalar",
            "taken_at": "2026-10-24T20:01:00+00:00"
        },
        {
            "pk": "4010000000000000006",
            "code": "Den_TahO0Hi",
            "permalink": "https://www.instagram.com/reel/Den_TahO0Hi/",
            "views": 164,
            "likes": 8,
            "comments": 2,
            "title": "Dasturlash va IT xizmatlari",
            "taken_at": "2026-10-18T10:00:00+00:00"
        }
    ]
}

conn = sqlite3.connect("data.db")
c = conn.cursor()
c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", ("ig_cached_profile_stats", json.dumps(stats_data)))
conn.commit()
conn.close()
print("CACHED_STATS_SAVED_SUCCESS")

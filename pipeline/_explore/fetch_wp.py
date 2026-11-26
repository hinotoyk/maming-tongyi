import json, time, urllib.parse, urllib.request, html, os

BASE = "https://racingnews.hkjc.com/chinese/wp-json/wp/v2/posts"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
# 探索期用的「按关键词搜」抓取器，仅作参考保留；正式口径见 pipeline/s0_enumerate_all.py
OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "cache"
)
os.makedirs(OUT, exist_ok=True)


def fetch_all(search):
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"search": search, "per_page": 100, "page": page,
                                    "_fields": "id,date,link,title"})
        req = urllib.request.Request(BASE + "?" + q, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
            total = int(r.headers.get("X-WP-Total", 0))
        if not data:
            break
        for p in data:
            out.append({"id": p["id"], "date": p["date"], "link": p["link"],
                        "title": html.unescape(p["title"]["rendered"])})
        if page == 1:
            print(f"[{search}] total={total}", flush=True)
        if len(out) >= total or page >= 40:
            break
        page += 1
        time.sleep(2.0)  # AGENTS.md §2.5：HKJC 全域 ≥2s/请求
    return out


if __name__ == "__main__":
    terms = ["出賽馬匹名單", "宣佈出賽", "最後宣佈出賽", "獲選馬匹名單", "出賽名單"]
    allposts = {}
    for t in terms:
        posts = fetch_all(t)
        allposts[t] = posts
        dates = sorted(p["date"][:10] for p in posts)
        print(f"  fetched={len(posts)} range={dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}", flush=True)
    with open(os.path.join(OUT, "all_posts.json"), "w", encoding="utf-8") as f:
        json.dump(allposts, f, ensure_ascii=False, indent=1)

import json, collections, re
P = "Z:/IdeaProjects/kzn-web/tests/_tmp/hkjc_explore/all_posts.json"
d = json.load(open(P, encoding="utf-8"))
posts = {}
for term, arr in d.items():
    for p in arr:
        posts[p["id"]] = p
posts = list(posts.values())
print("union posts:", len(posts))

TARGET = [p for p in posts if "出賽馬匹名單" in p["title"]]
FINAL = [p for p in posts if "最後宣佈出賽" in p["title"] and "名單" in p["title"]]
print("title contains 出賽馬匹名單:", len(TARGET))
print("title contains 最後宣佈出賽+名單:", len(FINAL))
for label, arr in [("名單", TARGET), ("最後", FINAL)]:
    by_year = collections.Counter(p["date"][:4] for p in arr)
    print(f"\n== {label} by year ==", dict(sorted(by_year.items())))
    print("range:", min((p["date"] for p in arr), default="-"), "~", max((p["date"] for p in arr), default="-"))

print("\n== top 60 title prefixes (名單) ==")
pref = collections.Counter(re.split(r"[–\-—(（]", p["title"])[0].strip() for p in TARGET)
for k, v in pref.most_common(60):
    print(f"{v:4}  {k[:70]}")

print("\n== sample titles per year (名單, first 8 each) ==")
for y in sorted(set(p["date"][:4] for p in TARGET)):
    print("\n--", y)
    for p in [x for x in TARGET if x["date"][:4] == y][:8]:
        print("  ", p["date"][:10], p["title"][:90])

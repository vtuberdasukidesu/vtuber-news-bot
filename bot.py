import os, json, feedparser, requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
FEED = "https://vtubernews.jp/index.rdf"
STATE = "posted.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

first_run = not os.path.exists(STATE)
posted = json.load(open(STATE)) if not first_run else []

resp = requests.get(FEED, headers={"User-Agent": UA}, timeout=30)
resp.raise_for_status()
feed = feedparser.parse(resp.content)

for e in reversed(feed.entries[:15]):
    if e.link in posted:
        continue
    if not first_run:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": f"{e.title.strip()}\n{e.link}"},
            timeout=30,
        )
        r.raise_for_status()
    posted.append(e.link)

json.dump(posted[-200:], open(STATE, "w"), ensure_ascii=False)

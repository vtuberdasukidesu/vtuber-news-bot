import os, re, sys, json, html
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Comment

FEED = "https://vtubernews.jp/index.rdf"
STATE = "posted.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

COVER = "https://livedoor.blogimg.jp/vtubernews/imgs/2/b/2b911902.png"  # 文章里没有图时的兜底封面

SKIP = {"script", "style", "mark", "noscript"}
BLOCKS = {"div", "dl", "dt", "dd", "p", "li", "ul", "ol", "section",
          "article", "table", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6"}


def youtube_id(src):
    m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{6,})", src or "")
    return m.group(1) if m else None


VIDEO_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\s\"'<>]*?&)?v=|embed/|live/|shorts/)"
    r"|youtu\.be/|[?&]yt=)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
URL_RE = re.compile(r"(https?://[^\s<>\"']+)")
NOISE_RES = [
    re.compile(r"^\d+\.\s*名無し\S*$"),
    re.compile(r"^\d{4}/\d{1,2}/\d{1,2}\(.\)\s*[\d:.]+(\s+ID:\S+)?$"),
]


YT_LINK_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^\s\"'<>]*?&)?v=|embed/|live/|shorts/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
T_RE = re.compile(r"[?&#](?:t|start)=([0-9hms]+)")


def to_seconds(v):
    if v.isdigit():
        return int(v)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", v)
    if not m or not any(m.groups()):
        return None
    h, mi, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + sec


def yt_link(href):
    """YouTube 链接 → (视频ID, 秒数或None, 规范短链接 youtu.be/ID?t=秒)；不是 YouTube 则返回 None"""
    m = YT_LINK_RE.search(href or "")
    if not m:
        return None
    vid = m.group(1)
    tm = T_RE.search(href)
    t = to_seconds(tm.group(1)) if tm else None
    return vid, t, f"https://youtu.be/{vid}" + (f"?t={t}" if t else "")


def video_times(html_text):
    """文章里每个视频出现过的时间戳 {视频ID: {秒数,...}}"""
    times = {}
    for a in BeautifulSoup(html_text or "", "html.parser").find_all("a", href=True):
        y = yt_link(a["href"])
        if y and y[1]:
            times.setdefault(y[0], set()).add(y[1])
    return times


def video_ids(html_text, limit=3):
    seen = []
    for m in VIDEO_RE.finditer(html_text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen[:limit]


def video_block(vid, t=None):
    """Telegraph 页里放封面图 + 跳转链接（Telegraph 的 YouTube 嵌入会报错 153）"""
    return [
        {"tag": "figure", "children": [
            {"tag": "img", "attrs": {"src": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}}]},
        {"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": f"https://youtu.be/{vid}" + (f"?t={t}" if t else "")},
             "children": ["▶ YouTube で再生" + (" （該当シーンから）" if t else "")]}]},
    ]


def plain_text(block):
    out = []
    for c in block.get("children", []):
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, dict):
            out.append(plain_text(c))
    return "".join(out).strip()


def good_href(href):
    return href.startswith("http") and not re.search(r"[\[\]() ]", href) and len(href) < 500


def to_nodes(content_html):
    """把文章 HTML 转成 Telegraph 能接受的节点列表"""
    soup = BeautifulSoup(content_html, "html.parser")
    blocks, cur = [], []

    def flush():
        nonlocal cur
        while cur and isinstance(cur[0], dict) and cur[0]["tag"] == "br":
            cur.pop(0)
        while cur and isinstance(cur[-1], dict) and cur[-1]["tag"] == "br":
            cur.pop()
        if cur:
            blocks.append({"tag": "p", "children": cur})
        cur = []

    def visit(el):
        if isinstance(el, Comment):
            return
        if isinstance(el, NavigableString):
            t = re.sub(r"\s+", " ", str(el))
            if t.strip():
                for part in URL_RE.split(t):
                    if URL_RE.fullmatch(part) and good_href(part):
                        y = yt_link(part)
                        cur.append({"tag": "a", "attrs": {"href": y[2] if y else part},
                                    "children": [part]})
                    elif part:
                        cur.append(part)
            return
        name = el.name
        if name in SKIP:
            return
        if name == "br":
            cur.append({"tag": "br"})
            return
        if name == "iframe":
            return
        if name == "img":
            src = el.get("src", "")
            if src.startswith("http") and "twi-user-img" not in (el.get("class") or []):
                flush()
                blocks.append({"tag": "figure", "children": [
                    {"tag": "img", "attrs": {"src": src}}]})
            return
        if name == "a":
            if el.find("img") or el.find("iframe"):
                for c in list(el.children):
                    visit(c)
                return
            text = el.get_text(" ", strip=True)
            if not text or text == "続きを読む":
                return
            href = el.get("href", "")
            y = yt_link(href)
            if y:
                href = y[2]
            if good_href(href):
                cur.append({"tag": "a", "attrs": {"href": href}, "children": [text]})
            else:
                cur.append(text)
            return
        if name == "blockquote":
            flush()
            start = len(blocks)
            for c in list(el.children):
                visit(c)
            flush()
            for b in blocks[start:]:
                if b["tag"] == "p":
                    b["tag"] = "blockquote"
            return
        if name in BLOCKS:
            flush()
            for c in list(el.children):
                visit(c)
            flush()
            return
        for c in list(el.children):
            visit(c)

    for child in list(soup.children):
        visit(child)
    flush()
    return blocks


def body_html_of(entry):
    if entry.get("content"):
        return entry.content[0].value
    if entry.get("summary"):
        return entry.summary
    return ""


def build_page_nodes(entry):
    body_html = body_html_of(entry)
    nodes = [b for b in to_nodes(body_html)
             if not any(r.match(plain_text(b)) for r in NOISE_RES)][:60]
    times = video_times(body_html)
    videos = []
    for v in video_ids(body_html):
        ts = times.get(v, set())
        videos += video_block(v, next(iter(ts)) if len(ts) == 1 else None)
    has_image = bool(videos) or any(b.get("tag") == "figure" for b in nodes)
    cover = [] if has_image else [{"tag": "figure", "children": [
        {"tag": "img", "attrs": {"src": COVER}}]}]
    head = [{"tag": "p", "children": [
        "出典: ", {"tag": "a", "attrs": {"href": entry.link}, "children": ["VTuberNews"]}]}]
    tail = [{"tag": "hr"}, {"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": entry.link},
         "children": ["▶ 全文・コメントは元記事で読む"]}]}]
    all_nodes = head + cover + videos + nodes + tail
    while len(json.dumps(all_nodes, ensure_ascii=False)) > 55000 and len(nodes) > 1:
        nodes.pop()
        all_nodes = head + cover + videos + nodes + tail
    return all_nodes


def telegraph_token():
    r = requests.post("https://api.telegra.ph/createAccount", data={
        "short_name": "vtubernews", "author_name": "VTuberNews"}, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j)
    return j["result"]["access_token"]


def make_page(token, title, nodes):
    r = requests.post("https://api.telegra.ph/createPage", data={
        "access_token": token,
        "title": title[:256],
        "author_name": "VTuberNews",
        "author_url": "https://vtubernews.jp/",
        "content": json.dumps(nodes, ensure_ascii=False),
    }, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j)
    return j["result"]["url"]


def tg(bot_token, method, data):
    r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}",
                      data=data, timeout=30)
    r.raise_for_status()


def send(bot_token, chat, title, page_url, vid=None):
    # 文字只有标题；末尾放一个看不见的链接，卡片（带缩略图）由 Telegram 生成，点卡片打开即时预览
    text = f'{html.escape(title)}<a href="{page_url}">&#8203;</a>'
    tg(bot_token, "sendMessage", {
        "chat_id": chat, "parse_mode": "HTML", "text": text,
        "link_preview_options": json.dumps({"url": page_url, "prefer_large_media": True})})


def main():
    bot_token = os.environ["BOT_TOKEN"]
    chat = os.environ["CHAT_ID"]

    first_run = not os.path.exists(STATE) or os.path.getsize(STATE) < 2
    posted = [] if first_run else json.load(open(STATE))

    resp = requests.get(FEED, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    tg_token = None
    failed = False
    for e in reversed(feed.entries[:15]):
        if e.link in posted:
            continue
        if not first_run:
            title = e.title.strip()
            url = e.link  # Telegraph 失败时，退回发原链接
            try:
                if tg_token is None:
                    tg_token = telegraph_token()
                url = make_page(tg_token, title, build_page_nodes(e))
            except Exception as ex:
                print("Telegraph 失败，改发原链接:", ex)
            try:
                vids = video_ids(body_html_of(e))
                send(bot_token, chat, title, url, vids[0] if vids else None)
            except Exception as ex:
                print("发送失败:", ex)
                failed = True
                break
        posted.append(e.link)

    json.dump(posted[-200:], open(STATE, "w"), ensure_ascii=False)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

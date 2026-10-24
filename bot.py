import os, re, sys, json, html, time
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Comment

FEED = "https://vtubernews.jp/index.rdf"
STATE = "posted.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

COVER = "https://livedoor.blogimg.jp/vtubernews/imgs/2/b/2b911902.png"  # 文章里没有图时的兜底封面

# 图片代理开关：如果你的电脑端打不开 i.ytimg.com / pbs.twimg.com（需要翻墙的域名），
# 导致 Telegraph 里的视频封面、推特图片丢失，可以把 False 改成 True，让图片走 images.weserv.nl 中转
IMG_PROXY = False

SKIP = {"script", "style", "mark", "noscript"}
BLOCKS = {"div", "dl", "dt", "dd", "p", "li", "ul", "ol", "section",
          "article", "table", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6"}

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
# 控制字符、零宽字符、方向控制字符：Telegraph 偶尔会因为它们保存失败
BAD_CHARS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff\ufff0-\uffff]")


def clean(s):
    """去掉可能导致保存失败的字符，以及孤立的代理字符"""
    s = BAD_CHARS_RE.sub("", s or "")
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")


def img_url(url):
    """推特图用 large 尺寸（orig 可能太大）；开启代理开关时走 weserv 中转"""
    url = url.replace("name=orig", "name=large")
    if IMG_PROXY and not url.startswith("https://images.weserv.nl/"):
        return "https://images.weserv.nl/?url=" + quote(url, safe="") + "&w=1280"
    return url


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
            {"tag": "img", "attrs": {"src": img_url(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")}}]},
        {"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": f"https://youtu.be/{vid}" + (f"?t={t}" if t else "")},
             "children": ["▶ YouTube で再生"]}]},
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
                    {"tag": "img", "attrs": {"src": img_url(src)}}]})
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


def prune(nodes):
    """发送前的清洗：去掉非法字符、空节点、缺少地址的链接和图片"""
    out = []
    for n in nodes:
        if isinstance(n, str):
            s = clean(n)
            if s:
                out.append(s)
            continue
        tag = n.get("tag")
        attrs = {k: clean(v) for k, v in (n.get("attrs") or {}).items()}
        if tag in ("br", "hr"):
            out.append({"tag": tag})
            continue
        if tag == "img":
            if attrs.get("src", "").startswith("http"):
                out.append({"tag": "img", "attrs": attrs})
            continue
        kids = prune(n.get("children", []))
        if not kids:
            continue
        if tag == "a" and not attrs.get("href", "").startswith("http"):
            out.extend(kids)  # 没有有效地址的链接，退化成普通文字
            continue
        if tag in ("p", "blockquote") and all(isinstance(k, dict) and k["tag"] == "br" for k in kids):
            continue
        node = {"tag": tag, "children": kids}
        if attrs:
            node["attrs"] = attrs
        out.append(node)
    return out


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
        {"tag": "img", "attrs": {"src": img_url(COVER)}}]}]
    
    tail = [{"tag": "hr"}, {"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": entry.link},
         "children": ["コメントは元記事で読む"]}]}]
    all_nodes = cover + videos + nodes + tail
    while len(json.dumps(all_nodes, ensure_ascii=False)) > 55000 and len(nodes) > 1:
        nodes.pop()
        all_nodes = cover + videos + nodes + tail
    return prune(all_nodes)


def simplify(nodes):
    """精简版：只留文字、视频封面和 youtu.be / 原站链接，用于完整版反复失败时"""
    def flat(children):
        out = []
        for c in children:
            if isinstance(c, str):
                out.append(c)
            elif c.get("tag") == "br":
                out.append(c)
            elif c.get("tag") == "a":
                href = c.get("attrs", {}).get("href", "")
                if href.startswith("https://youtu.be/") or "vtubernews.jp" in href:
                    out.append(c)
                else:
                    out.append(plain_text(c))
            else:
                out.extend(flat(c.get("children", [])))
        return out

    out = []
    for n in nodes:
        t = n.get("tag")
        if t == "figure":
            if any("ytimg.com" in c.get("attrs", {}).get("src", "") for c in n["children"]):
                out.append(n)
        elif t == "hr":
            out.append(n)
        elif t in ("p", "blockquote"):
            kids = flat(n.get("children", []))
            if kids:
                out.append({"tag": "p", "children": kids})
    return prune(out)


def minimal_nodes(entry, title):
    """最小版：只有出处、标题和原文链接，保证页面能建出来"""
    return prune([
        {"tag": "p", "children": ["出典: ", {"tag": "a", "attrs": {"href": entry.link},
                                            "children": ["VTuberNews"]}]},
        {"tag": "p", "children": [title]},
        {"tag": "p", "children": [{"tag": "a", "attrs": {"href": entry.link},
                                   "children": ["コメントは元記事で読む"]}]},
    ])


def shorten(title, n):
    title = clean(title)
    return title if len(title) <= n else title[:n - 1] + "…"


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
        "title": title,
        "author_name": "VTuberNews",
        "author_url": "https://vtubernews.jp/",
        "content": json.dumps(nodes, ensure_ascii=False),
    }, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j)
    return j["result"]["url"]


_TOKEN = None


def get_token(reset=False):
    global _TOKEN
    if reset:
        _TOKEN = None
    if _TOKEN is None:
        _TOKEN = telegraph_token()
    return _TOKEN


def publish_page(title, full, simple, minimal):
    """创建 Telegraph 页面，逐级降级：完整版×2 → 精简版（短标题、新账号）→ 最小版（更短标题）"""
    plans = [("完整版", full, 256), ("完整版", full, 256),
             ("精简版", simple, 60), ("最小版", minimal, 30)]
    last = None
    for i, (name, nodes, tlen) in enumerate(plans):
        if i:
            time.sleep([0, 5, 10, 20][i])
        t = shorten(title, tlen)
        try:
            return make_page(get_token(reset=(i == 2)), t, nodes)
        except Exception as ex:
            last = ex
            print(f"Telegraph 第 {i + 1} 次失败（{name}，标题 {len(t)} 字，"
                  f"节点 {len(nodes)} 个，{len(json.dumps(nodes, ensure_ascii=False))} 字符）: {ex}")
            m = re.search(r"FLOOD_WAIT_(\d+)", str(ex))
            if m:
                time.sleep(min(int(m.group(1)), 60))
    print("最后一次尝试的内容（前 500 字符）:", json.dumps(plans[-1][1], ensure_ascii=False)[:500])
    raise last


def tg(bot_token, method, data):
    """调用 Telegram 接口：超时或失败时自动重试（极少数情况下可能因此重复发出一条）"""
    last = None
    for i, wait in enumerate([0, 3, 8]):
        if wait:
            time.sleep(wait)
        try:
            r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}",
                              data=data, timeout=45)
            if r.status_code == 429:
                try:
                    ra = int(r.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    ra = 5
                time.sleep(min(ra, 30))
                last = RuntimeError("429 Too Many Requests")
                continue
            r.raise_for_status()
            return
        except Exception as ex:
            last = ex
            print(f"Telegram 第 {i + 1} 次请求失败: {ex}")
    raise last


def send(bot_token, chat, title, page_url, vid=None):
    # 文字只有标题；末尾放一个看不见的链接，卡片（带缩略图）由 Telegram 生成，点卡片打开即时预览
    text = f'{html.escape(clean(title))}<a href="{page_url}">&#8203;</a>'
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

    failed = False
    for e in reversed(feed.entries[:15]):
        if e.link in posted:
            continue
        if not first_run:
            title = clean(e.title).strip()
            url = e.link  # Telegraph 实在失败时，退回发原链接
            try:
                full = build_page_nodes(e)
                url = publish_page(title, full, simplify(full), minimal_nodes(e, title))
            except Exception as ex:
                print(f"Telegraph 最终失败，改发原链接（{title[:30]}）:", ex)
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

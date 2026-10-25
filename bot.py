import os, re, sys, json, html, time, copy
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup, NavigableString, Comment

FEED = "https://vtubernews.jp/index.rdf"
STATE = "posted.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

COVER = "https://livedoor.blogimg.jp/vtubernews/imgs/2/b/2b911902.png"  # 文章里没有图时的兜底封面

# ── 图片显示相关开关 ──────────────────────────────────────────────
# 1) UPLOAD_IMAGES：先尝试把图片转存到 Telegraph 自己的服务器（这样哪里都能看到）。
#    这个上传接口是非官方的，据说可能已被关闭；失败会自动跳过，不影响发帖。
UPLOAD_IMAGES = True
# 2) IMG_PROXY：转存失败时，图片改走 images.weserv.nl 中转，解决电脑端打不开
#    i.ytimg.com / pbs.twimg.com 导致正文无图的问题。想完全用原图地址就改成 False。
IMG_PROXY = True
MAX_UPLOADS = 40  # 每次运行最多转存多少张图
# ────────────────────────────────────────────────────────────────

# 发帖防重复开关：
# False（推荐）：Telegram 超时、说不准消息有没有发出去时，不重试，按“已发送”处理（不会重复发帖，
#               但极少数情况下如果消息其实没发出去，这篇会被漏掉）
# True：这种情况也重试（不会漏发，但可能在频道里重复发一条）
RETRY_ON_AMBIGUOUS = False

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


def fix_img(url):
    """推特图用 large 尺寸（orig 可能太大）"""
    return url.replace("name=orig", "name=large")


def proxy_url(url):
    return "https://images.weserv.nl/?url=" + quote(url, safe="") + "&w=1280"


# ── 图片转存 / 代理（在创建 Telegraph 页面前统一处理，结果有缓存）──────────
_IMG_CACHE = {}
_UPLOAD_FAILS = 0
_UPLOADED = 0


def upload_image(url):
    """下载图片并传到 Telegraph；失败返回 None。连续失败 2 次后本次运行不再尝试"""
    global _UPLOAD_FAILS, _UPLOADED
    if _UPLOAD_FAILS >= 2 or _UPLOADED >= MAX_UPLOADS:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        data = r.content
        if not data or len(data) > 5_000_000:
            return None  # 空文件或超过 5MB，不算上传接口的问题
        ctype = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        ext = {"image/png": "png", "image/gif": "gif", "image/jpeg": "jpg"}.get(ctype)
        if ext is None:
            return None  # Telegraph 只收 jpg/png/gif
        up = requests.post("https://telegra.ph/upload",
                           files={"file": (f"img.{ext}", data, ctype)}, timeout=30)
        try:
            src = up.json()[0]["src"]
        except Exception:
            raise RuntimeError(f"上传接口返回异常: HTTP {up.status_code} {up.text[:100]}")
        if not str(src).startswith("/file/"):
            raise RuntimeError(f"意外的返回: {up.text[:100]}")
        _UPLOAD_FAILS = 0
        _UPLOADED += 1
        return src
    except Exception as ex:
        _UPLOAD_FAILS += 1
        print(f"图片转存失败（{url[:60]}）: {ex}")
        return None


def final_src(src):
    if src in _IMG_CACHE:
        return _IMG_CACHE[src]
    out = None
    if UPLOAD_IMAGES and src.startswith("http"):
        out = upload_image(src)
    if not out:
        out = proxy_url(src) if (IMG_PROXY and src.startswith("http")) else src
    _IMG_CACHE[src] = out
    return out


def finalize_images(nodes):
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
            continue
        n = dict(n)
        if n.get("tag") == "img":
            n["attrs"] = {**n["attrs"], "src": final_src(n["attrs"]["src"])}
        elif "children" in n:
            n["children"] = finalize_images(n["children"])
        out.append(n)
    return out
# ────────────────────────────────────────────────────────────────


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
                    {"tag": "img", "attrs": {"src": fix_img(src)}}]})
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
            if attrs.get("src", "").startswith(("http", "/file/")):
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
        {"tag": "img", "attrs": {"src": COVER}}]}]

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
    """最小版：只有标题和原文链接，保证页面能建出来"""
    return prune([
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
    """创建 Telegraph 页面，逐级降级。

    从日志看，标题过长（约 70 字以上）时 Telegraph 会返回 PAGE_SAVE_FAILED，
    所以页面标题一开始就截到 60 字（频道消息里仍然是完整标题）。
    顺序：完整版/60字 → 完整版/45字+新账号 → 精简版/45字 → 最小版/30字
    """
    plans = [("完整版", full, 60, False), ("完整版", full, 45, True),
             ("精简版", simple, 45, False), ("最小版", minimal, 30, False)]
    last = None
    for i, (name, nodes, tlen, new_acc) in enumerate(plans):
        if i:
            time.sleep([0, 5, 10, 20][i])
        t = shorten(title, tlen)
        try:
            url = make_page(get_token(reset=new_acc), t, finalize_images(nodes))
            if i:
                print(f"Telegraph 在第 {i + 1} 次（{name}，标题 {len(t)} 字）才成功，内容可能不完整")
            return url
        except Exception as ex:
            last = ex
            print(f"Telegraph 第 {i + 1} 次失败（{name}，标题 {len(t)} 字，"
                  f"节点 {len(nodes)} 个，{len(json.dumps(nodes, ensure_ascii=False))} 字符）: {ex}")
            m = re.search(r"FLOOD_WAIT_(\d+)", str(ex))
            if m:
                time.sleep(min(int(m.group(1)), 60))
    print("最后一次尝试的内容（前 500 字符）:", json.dumps(plans[-1][1], ensure_ascii=False)[:500])
    raise last


def _not_sent(ex):
    """能确定请求根本没送到 Telegram 的错误"""
    s = str(ex)
    return (isinstance(ex, requests.exceptions.ConnectTimeout)
            or "Failed to establish a new connection" in s
            or "Temporary failure in name resolution" in s)


def tg(bot_token, method, data):
    """调用 Telegram 接口。
    - 请求没送到（连不上）、429：消息肯定没发出去，自动重试
    - 读超时 / 连接中途断开 / 5xx：请求可能已经送到，消息说不定已经发出去了。
      默认不重试，记为已发送（避免频道里出现重复帖）
    - 其他错误（如 400 参数错误）：直接报错
    """
    last = None
    for i, wait in enumerate([0, 3, 8]):
        if wait:
            time.sleep(wait)
        try:
            r = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}",
                              data=data, timeout=45)
        except Exception as ex:
            if _not_sent(ex):
                last = ex
                print(f"Telegram 第 {i + 1} 次连接失败（消息没发出，重试）: {ex}")
                continue
            print(f"Telegram 请求中断，消息可能已经发出: {ex}")
            if RETRY_ON_AMBIGUOUS:
                last = ex
                continue
            return
        if r.status_code == 429:
            try:
                ra = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                ra = 5
            print(f"Telegram 限流，等待 {min(ra, 30)} 秒后重试")
            time.sleep(min(ra, 30))
            last = RuntimeError("429 Too Many Requests")
            continue
        if r.status_code >= 500:
            print(f"Telegram 返回 {r.status_code}，消息可能已经发出")
            if RETRY_ON_AMBIGUOUS:
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            return
        r.raise_for_status()
        return
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

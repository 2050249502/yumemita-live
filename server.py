#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""梦限大直播站 · 云端抓取模块（2026-10-17）

从本机 server.py 里**机械抽取**出来的最小闭包：只保留 4 个公开数据源抓取函数
（get_status / get_x_schedule / get_upcoming / get_bilibili）及其全部依赖，
去掉了 HTTP 服务、弹幕、内嵌播放器、B站弹窗渲染等一切与本功能无关的代码。

抽法：用 AST 算出四个入口的名字闭包，再按行原样搬运（不手抄 ⇒ 不会移植走样）。
验证：与 server.py 版同时跑一次，逐字段比对输出一致。

代理：只认环境变量（HTTPS_PROXY / YUMEMITA_PROXY），留空即直连——
云端 runner 本身在墙外，不需要代理；本机跑要给环境变量。
"""
import json, os, re, threading, time, urllib.request, urllib.error
from urllib.parse import quote

ROOT = os.path.dirname(os.path.abspath(__file__))


CH_CACHE = os.path.join(ROOT, "channels.json")


RECENT_FILE = os.path.join(ROOT, "recent_cache.json")


MEMBERS = [
    {"id": "arale",  "jp": "仲町あられ", "role": "Vo.",    "handle": "@arale_yumemita"},
    {"id": "nonoka", "jp": "宮永ののか", "role": "Gt.",    "handle": "@nonoka_yumemita"},
    {"id": "ritsu",  "jp": "峰月律",     "role": "Gt.",    "handle": "@ritsu_yumemita"},
    {"id": "miyako", "jp": "藤都子",     "role": "Key.",   "handle": "@miyako_yumemita"},
    {"id": "yuno",   "jp": "千石ユノ",   "role": "DJ&Mp.", "handle": "@yuno_yumemita"},
    {"id": "official", "jp": "夢限大みゅーたいぷ", "role": "公式", "handle": "@bdp_yumemita"},
]


_lock = threading.Lock()


BT_KW = ("夢限大みゅーたいぷ", "梦限大", "ゆめみた")   # 日文官方名 + 中文俗称


BT_KW_EXTRA = ("峰月律", "仲町あられ", "仲町阿拉蕾", "宮永ののか", "千石ユノ")


BT_HINT = ("梦限大", "夢限大", "ゆめみた", "みゅーたいぷ",
           "仲町あられ", "阿拉蕾", "宮永ののか", "宫永野乃花", "宮永野乃花",
           "峰月律", "藤都子", "千石ユノ", "バンドリ", "BanG Dream", "bangdream")


BT_TTL = 900                 # 15 分钟缓存（B站搜索有风控，别打太勤）


BT_MAX = 60        # ★ 2026-10-15：启用白名单后 kept 常 >30（实测 47），30 条会把名单内较早的投稿截掉


_bt_cache = {"ts": 0.0, "list": []}


BT_UPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_ups.txt")


_bt_ups_cache = {"mtime": -1.0, "uids": set(), "names": set()}


BT_PAGES = 2                 # ★ 每关键词翻几页：第一页 20 条很容易把白名单内投稿挤出去


BT_COOLDOWN = 300            # ★ 全失败后的冷却秒数：别越打越被风控（也保护 B站）


_bt_last = {"ts": 0.0, "errs": [], "raw": 0, "kept": 0, "cool": 0.0}


BT_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
)


_bt_ua_i = 0


def _load_bt_ups():
    """读 bt_ups.txt（按 mtime 缓存）。返回 (uids:set[str], names:set[str])。"""
    try:
        mt = os.path.getmtime(BT_UPS_FILE)
    except OSError:
        return set(), set()
    if mt == _bt_ups_cache["mtime"]:
        return _bt_ups_cache["uids"], _bt_ups_cache["names"]
    uids, names = set(), set()
    try:
        with open(BT_UPS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                m = re.search(r"space\.bilibili\.com/(\d+)", line)
                if m:
                    uids.add(m.group(1))
                elif re.fullmatch(r"\d{3,}", line):
                    uids.add(line)
                else:
                    names.add(line.lower())
    except OSError:
        pass
    # ★ 白名单变了 ⇒ 立刻作废列表缓存（否则改了 bt_ups.txt 要等 BT_TTL 15 分钟才见效）
    _bt_cache["ts"] = 0.0
    _bt_ups_cache.update({"mtime": mt, "uids": uids, "names": names})
    return uids, names


def _bj_dt(ts):
    """unix 秒 → 北京时间 'MM-DD HH:MM'（B站给的是 UTC 秒）"""
    t = time.gmtime(int(ts or 0) + 8 * 3600)
    return time.strftime("%m-%d %H:%M", t)


def _bt_search(kw, page=1):
    """B站视频搜索（order=pubdate 最新优先）。★ 直连，不走代理。"""
    global _bt_ua_i
    _bt_ua_i += 1
    ua = BT_UA_POOL[_bt_ua_i % len(BT_UA_POOL)]
    url = ("https://api.bilibili.com/x/web-interface/wbi/search/type"
           "?search_type=video&order=pubdate&page=%d&keyword=%s" % (page, quote(kw)))
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    if d.get("code") != 0:
        raise RuntimeError("bilibili code=%s msg=%s" % (d.get("code"), d.get("message")))
    res = (d.get("data") or {}).get("result") or []
    if not res:
        # ★ code:0 + 空结果 = 软风控（不是"真没视频"）—— 抛出来，让 _bt_all 记进 diag 并进冷却
        raise RuntimeError("empty result (code=0) — likely UA/ip soft rate-limit")
    return res


def _bt_all():
    """抓 B站（关键词 × 翻页）→ 过滤 → 返回。
    ★ 2026-10-15 修：原来 `except: continue` 把失败**静默吞掉**，抓失败时上层只能回退旧缓存
    —— 面板看着"有数据"，其实全是上一轮的（改白名单后表现为"改动没生效"）。
    现在把 raw/errs 记进 _bt_last 并暴露到 /api/bilibili 的 diag，肉眼可见。"""
    seen, items = set(), []
    errs, raw = [], 0
    wl_uids, wl_names = _load_bt_ups()
    whitelist = bool(wl_uids or wl_names)
    # ★ 白名单模式才追加成员名关键词（补漏）；老行为下不加，避免噪音
    kws = ([(kw, BT_PAGES) for kw in BT_KW]
           + ([(kw, 1) for kw in BT_KW_EXTRA] if whitelist else []))
    for kw, pages in kws:
        for pg in range(1, pages + 1):
            try:
                res = _bt_search(kw, pg)
            except Exception as e:
                errs.append("%s/p%d: %s" % (kw, pg, str(e)[:60]))
                continue
            raw += len(res)
            for r in res:
                bv = str(r.get("bvid") or "")
                if not bv or bv in seen:
                    continue
                title = re.sub(r"<[^>]+>", "", str(r.get("title") or ""))
                author = str(r.get("author") or "")
                desc = str(r.get("description") or "")
                blob = (title + " " + author + " " + desc).lower()
                if whitelist:
                    # ★ 白名单模式：只认名单内的 UP，且不再要求标题命中 BT_HINT
                    if not (str(r.get("mid")) in wl_uids or author.lower() in wl_names):
                        continue
                elif not any(h.lower() in blob for h in BT_HINT):
                    continue                  # ★ 相关性过滤（白名单为空时的老行为）
                seen.add(bv)
                items.append({
                    "bvid": bv,
                    "title": title,
                    "author": author,
                    "mid": r.get("mid"),
                    "ts": int(r.get("pubdate") or 0),
                    "wh": _bj_dt(r.get("pubdate")),
                    "dur": str(r.get("duration") or ""),
                    "play": r.get("play"),
                    "url": "https://www.bilibili.com/video/" + bv,
                })
            time.sleep(0.35)                  # 温柔点，防 412
    if errs and raw == 0:
        _bt_last["cool"] = time.time() + BT_COOLDOWN
    _bt_last.update({"ts": time.time(), "errs": errs[-4:], "raw": raw,
                     "kept": len(items), "wl": len(wl_uids) + len(wl_names)})
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return items[:BT_MAX]


def get_bilibili(force=False):
    """B站搬运列表。抓失败就回上次成功的（同站内其它栏的惯例，不让面板挂掉）。"""
    now = time.time()
    if not force and _bt_cache["list"] and (now - _bt_cache["ts"]) < BT_TTL:
        return _bt_cache["list"]
    if not force and now < _bt_last.get("cool", 0):
        return _bt_cache["list"] or []        # ★ 冷却期内不真抓（刚被风控过）
    try:
        out = _bt_all()
        if out:
            _bt_cache.update({"ts": now, "list": out})
            return out
    except Exception as e:
        _bt_last["errs"] = [str(e)[:80]]
    return _bt_cache["list"] or []


PROXY = os.environ.get("YUMEMITA_PROXY", "")   # 留空 = 直连；云端 runner 本就在墙外，不需要代理


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _proxy():
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return PROXY or None


def fetch(url, timeout=12):
    """返回 (ok, text_or_errmsg, final_url)。失败绝不抛。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
    })
    p = _proxy()
    if p:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连
    try:
        with op.open(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace"), r.geturl()
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), url


def load_channels():
    try:
        with open(CH_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def save_channels(d):
    try:
        with open(CH_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


CHANNELS = load_channels()


def resolve_ucid(m):
    """解析 YouTube handle -> UCID。失败返回 (None, 原因)。结果写缓存。

    ★ 2026-10-13 实测教训：不能用「页面里第一个 "channelId":"UC..."」——
      那往往不是该频道自己的 ID（实测 5 个不同 handle 抓到同一个 ID）。
      正解 = "externalId"（频道自身 ID），其次 canonical link / og:url。
    """
    hit = CHANNELS.get(m["handle"])
    if hit and hit.get("ucid"):
        return hit["ucid"], None
    ok, html, _final = fetch("https://www.youtube.com/%s" % m["handle"])
    if not ok:
        return None, html
    for pat in (
        r'"externalId":"(UC[\w-]{22})"',                                             # ★ 最准
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'"browseId":"(UC[\w-]{22})"',
    ):
        mt = re.search(pat, html)
        if mt:
            CHANNELS[m["handle"]] = {"ucid": mt.group(1)}
            save_channels(CHANNELS)
            return mt.group(1), None
    return None, "页面里没找到频道 ID"


_status_cache = {"ts": 0.0, "data": None}


_status_busy = False          # ★ 防击穿：同一时刻只允许一个线程去真实抓取


STATUS_TTL = 60


def live_of(m):
    """True / False / None(未知)

    ★ 2026-10-16 第二轮口径（YouTube 又改版，老判据**永久失效**，实测过）：
      旧判据「/@handle/live 最终跳转是否变成 /watch?v=」已经不再成立 —— 实测四种姿势
      （@handle / channel/<ucid> / 带 Chrome UA / 加 hl=ja）**全部 HTTP 200、最终 URL 不变**，
      于是**正在播的频道被判成未开播**（实测：峰月律 22:01 开播，站点仍报 live=false、
      `_live_vids()` 恒为空 ⇒ 房间里「本站中转」永远接不上）。
      ★ 2026-10-17 补充：中转整条线已拆除（已决定不做视频流），`_live_vids()` 一并删掉；
        但**判活本身仍然是关键** —— 卡片上的「● 直播中」、房间自动挂 YouTube 嵌入都靠它。
      新主判据 = 解析页面里 **ytInitialPlayerResponse** 段：
          playabilityStatus.status == "OK"  且  liveBroadcastDetails.isLiveNow == true
          → 在播；vid 取 videoDetails.videoId
      ⚠️ 必须**只在 playerResponse 段内**判，不能扫整页：
        整页里的 "isLive":true / "isLiveContent":true 在未开播页也存在（老假阳性来源）。
      ★ 实测区分度（2026-10-16 22:35，同一时刻）：
          在播 ritsu          → isLiveNow=true  / playability=OK
          未播 arale/nonoka/yuno → isLiveNow=false / playability=LIVE_STREAM_OFFLINE（排了预告）
          未播 miyako          → 整页没有 playerResponse
        三种非在播情形全部判 False ✓
      老跳转判据保留为**兜底**（万一 YouTube 改回去）。
    """
    ok, html, final = fetch("https://www.youtube.com/%s/live" % m["handle"])
    if not ok:
        ucid = CHANNELS.get(m["handle"], {}).get("ucid")
        if ucid:
            ok, html, final = fetch("https://www.youtube.com/channel/%s/live" % ucid)
        if not ok:
            return None
    # ── 主判据（2026-10-16）：playerResponse 里的 isLiveNow + playabilityStatus ──
    mp = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});", html, re.S)
    if mp:
        pr = mp.group(1)
        ms = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', pr)
        ml = re.search(r'"liveBroadcastDetails":\{"isLiveNow":(true|false)', pr)
        mv = re.search(r'"videoDetails":\{"videoId":"([A-Za-z0-9_-]{11})"', pr)
        if ml and ml.group(1) == "true" and (not ms or ms.group(1) == "OK"):
            # 返回字符串 = 正在播 + 有视频 ID（调用方只判真假时照样成立）
            return mv.group(1) if mv else True
        return False
    # ── 兜底：老口径（页面没 playerResponse 时，才看跳转）──
    if "/watch?v=" in final:
        mm = re.search(r"/watch\?v=([A-Za-z0-9_-]{6,})", final)
        return mm.group(1) if mm else True
    return False


_x_cache = {"ts": 0.0, "data": None}


X_TTL = 900           # ★ 15 分钟（原 300）。配信表一天只更新几次，5 分钟太勤：


X_USER = "BDP_yumemita"


JP2CN = {}


WEEK_CN = {"月": "周一", "火": "周二", "水": "周三", "木": "周四", "金": "周五", "土": "周六", "日": "周日"}


def jst_to_cst(hh, mm):
    """日本时间 → 北京时间（-1 小时）。返回 (时, 分, 跨天偏移 0/-1)。"""
    t = hh * 60 + mm - 60
    off = 0
    if t < 0:
        t += 1440
        off = -1
    return t // 60, t % 60, off


def _x_page():
    """取官方 X 的时间线（返回 (kind, content)，kind ∈ {"html","md"}）。

    ★ 2026-10-17 实测（GitHub Actions runner = 微软机房 IP，出口 20.55.95.27）：
        ① x.com 首屏      → 一律 403「Enable JavaScript」（三种 UA 都一样）
        ② syndication 时间线 → 429（机房 IP 被限流）
        ③ r.jina.ai 读 x.com → **200，markdown 正文里带配信表** ⇒ 云端唯一走得通的路
      本机（经代理）仍是 ① 最优先：字段最全、最快。

    自测：YUMEMITA_X_SOURCE = x|syndication|jina  可强制指定通道。
    """
    want = os.environ.get("YUMEMITA_X_SOURCE", "")
    if want in ("", "x"):
        ok, html, _f = fetch("https://x.com/%s" % X_USER, timeout=25)
        if ok and "full_text" in (html or ""):
            return ("html", html)
        if want == "x":
            return None
    if want in ("", "syndication"):
        for attempt in range(3):
            ok2, html2, _f2 = fetch(
                "https://syndication.twitter.com/srv/timeline-profile/screen-name/%s" % X_USER, timeout=25)
            if ok2 and "full_text" in (html2 or ""):
                return ("html", html2)
            time.sleep(2 + attempt * 2)
        if want == "syndication":
            return None
    # ③ r.jina.ai（第三方阅读器，从它自己的服务器取 x.com，绕开机房 IP 被挡）
    for attempt in range(2):
        ok3, md, _f3 = fetch("https://r.jina.ai/https://x.com/%s" % X_USER, timeout=40)
        if ok3 and "配信スケジュール" in (md or ""):
            return ("md", md)
        time.sleep(3 * (attempt + 1))
    return None


def _x_from_md(md):
    """从 r.jina.ai 的 markdown 里抠配信表（云端唯一可用通道）。

    markdown 形态（一条推文一行）：
        *   [@BDP_yumemita](…) [10h](https://x.com/BDP_yumemita/status/<ID>)  ／ 🛸#ゆめみた
            10/16(金)の配信スケジュール🌟 ＼ 【メン限】22:00～ 仲町あられ [youtube…](…) …

    两点与 HTML 通道不同，其余规则（JST→北京、中文译名、メン限、明日+1）完全沿用：
      · 推文时间靠**推文 ID 反解**（雪花算法：ms = (id >> 22) + 1288834974657）
      · 正文里的 markdown 链接要先剥掉，否则成员名会被 URL 污染
    """
    out = []
    for m in re.finditer(r"https://x\.com/[^/\s]+/status/(\d+)\)\s+([^\n]{0,3000})", md):
        tid, body = int(m.group(1)), m.group(2)
        if "配信スケジュール" not in body:
            continue
        txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body)          # 图片
        txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)        # 链接 → 文字
        txt = re.sub(r"\s*https?://\S+", " ", txt)                   # 残留裸 URL
        dm = (re.search(r"(\d{1,2})/(\d{1,2})[^\d]{0,4}\((.)\)[^\d]{0,4}の配信スケジュール", txt)
              or re.search(r"(\d{1,2})/(\d{1,2})[^\d]{0,4}の配信スケジュール", txt))
        date_cn = ""
        if dm:
            if dm.lastindex and dm.lastindex >= 3:
                date_cn = "%s月%s日(%s)" % (dm.group(1), dm.group(2), WEEK_CN.get(dm.group(3), dm.group(3)))
            else:
                date_cn = "%s月%s日" % (dm.group(1), dm.group(2))
        slots = []
        for sm in re.finditer(r"([^\d\n]{0,12}?)(\d{1,2}):(\d{2})\s*[～~]\s*([^\s\[\]（）()]+)", txt):
            pre, jh, jm = sm.group(1), int(sm.group(2)), int(sm.group(3))
            name_jp = sm.group(4).strip()
            if not name_jp:
                continue
            ch, cm2, off = jst_to_cst(jh, jm)
            if re.search(r"明日|翌日", pre):
                off += 1
            slots.append({
                "time": "%02d:%02d" % (ch, cm2),
                "time_jst": "%02d:%02d" % (jh, jm),
                "day_offset": off,
                "member_only": bool(re.search(r"メン限|メンバー限定", pre + name_jp)),
                "name": JP2CN.get(name_jp, name_jp),
                "name_jp": name_jp,
            })
        out.append({
            "date": ("%s/%s" % (dm.group(1), dm.group(2))) if dm else "",
            "date_cn": date_cn,
            "ts": ((tid >> 22) + 1288834974657) // 1000,
            "slots": slots,
            "note_cn": "※时间可能未经预告变更，请以官方为准",
            "text": txt.strip()[:700],
        })
    out.sort(key=lambda x: -x["ts"])
    return out or None


def x_schedule(limit=3):
    """从官方 X 首屏抠「N/M(曜)の配信スケジュール」推文。

    ★ 推文里的时间是**日本时间(JST, UTC+9)**；本函数统一换算成**北京时间(UTC+8, -1h)**，
      并把成员名译成中文（原名一并保留，因为译名非官方）。
    """
    got = _x_page()
    if not got:
        return None
    _kind, html = got
    if _kind == "md":                     # 走 r.jina.ai 的 markdown 通道（云端）
        return _x_from_md(html)
    # 推文正文的三种形态都收（优先级从上到下）：
    #   ① x.com 新形态：首屏 JSON 被再转义一层 ⇒  \"full_text\":\"…\"
    #   ② syndication 时间线：__NEXT_DATA__ 里的**裸 JSON** ⇒  "full_text":"…"
    #      （机房 IP 走这条路，见 _x_page()）
    #   ③ x.com 旧形态： full_text:"…"
    _mms = list(re.finditer(r'\\"full_text\\":\\"(.*?)\\",\\"hashtag_entities\\"', html, re.S))
    if not _mms:
        _mms = list(re.finditer(r'"full_text":"((?:[^"\\]|\\.)*)"', html))
    if not _mms:
        _mms = list(re.finditer(r'full_text:"((?:[^"\\]|\\.)*)"', html))
    out = []
    for mm in _mms:
        txt = mm.group(1)
        # 连续解转义：新形态需要两遍（\\n → \n → 真换行），旧形态第二遍是恒等，安全
        for _ in range(2):
            if "\\" not in txt:
                break
            try:
                _nxt = json.loads('"' + txt + '"')
            except Exception:
                break
            if _nxt == txt:
                break
            txt = _nxt
        txt = txt.replace("\\n", "\n")      # 兜底：残留的字面 \n
        if "配信スケジュール" not in txt:
            continue
        # ⚠️ created_at_ms 出现在 full_text **之前**（同一 entry 的 details 里），往前找
        # ★ 2026-10-16：新形态是 `\"created_at_ms\":1792…`，冒号前多一层转义引号 ⇒ 用 [^0-9]{0,4} 通吃两种
        head = html[max(0, mm.start() - 2000): mm.start()]
        cms = re.findall(r"created_at_ms[^0-9]{0,4}(\d+)", head)
        ts = int(cms[-1]) // 1000 if cms else 0
        dm = re.search(r"(\d{1,2})/(\d{1,2})[^\d]{0,4}\((.)\)[^\d]{0,4}の配信スケジュール", txt)
        # ★ 不要求带(曜)，退一步只匹配日期
        if not dm:
            dm = re.search(r"(\d{1,2})/(\d{1,2})[^\d]{0,4}の配信スケジュール", txt)
        date_cn = ""
        if dm:
            if dm.lastindex and dm.lastindex >= 3:
                date_cn = "%s月%s日(%s)" % (dm.group(1), dm.group(2), WEEK_CN.get(dm.group(3), dm.group(3)))
            else:
                date_cn = "%s月%s日" % (dm.group(1), dm.group(2))
        slots = []
        for line in txt.split("\n"):
            l = line.strip()
            # ★ 前缀放宽到 12 个非数字字符（原为 4）
            #   实测 2026-10-15：官方 10/14 表 3 场，站里只出 2 场 —— 藤都子那条写的是
            #   `【メン限】21:30～ 藤都子`，前缀 `【メン限】` 是 5 个字符 ⇒ `{0,4}` 整行不匹配被丢掉。
            mt2 = re.match(r"^[^\d]{0,12}(\d{1,2}):(\d{2})\s*[～~]\s*(.+?)\s*$", l)
            if not mt2:
                continue
            jh, jm = int(mt2.group(1)), int(mt2.group(2))
            name_jp = mt2.group(3).strip()
            ch, cm2, off = jst_to_cst(jh, jm)
            pre = l[:mt2.start(1)]                     # 时间之前的记号（☀ / 【メン限】 / 明日朝 …）
            # ★「☀明日朝6:30～」= 推文日的**次日**。原来只看钟点 ⇒ 次日早上那场被塞进当天表里，
            #   显示成"今天 05:30"（比 22:00 还早，看着像已过去）。这里补 +1 天。
            if re.search(r"明日|翌日", pre):
                off += 1
            # ★ 保留官方原文的「会员限定」信息（前端加小标签）
            member_only = bool(re.search(r"メン限|メンバー限定", pre + name_jp))
            slots.append({
                "time": "%02d:%02d" % (ch, cm2),          # ★ 北京时间
                "time_jst": "%02d:%02d" % (jh, jm),       # 原始日本时间
                "day_offset": off,                        # -1=北京在前一晚 / +1=次日
                "member_only": member_only,               # 【メン限】场次
                "name": JP2CN.get(name_jp, name_jp),      # ★ 中文译名（查不到就留原名）
                "name_jp": name_jp,
            })
        out.append({
            "date": ("%s/%s" % (dm.group(1), dm.group(2))) if dm else "",
            "date_cn": date_cn,
            "ts": ts,
            "slots": slots,
            "note_cn": "※时间可能未经预告变更，请以官方为准",
            "text": txt.strip()[:700],
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda x: -x["ts"])
    return out or None


def get_x_schedule(force=False):
    now = time.time()
    if (not force) and _x_cache["data"] is not None and (now - _x_cache["ts"]) < X_TTL:
        return _x_cache["data"]
    try:
        d = x_schedule()
    except Exception:
        d = None
    if d is not None:
        _x_cache["data"] = d
        _x_cache["ts"] = now
    return _x_cache["data"] if d is None else d


_UP_CACHE = {"ts": 0.0, "data": None}


UP_TTL = 900                     # 15 分钟，同 X_TTL


_up_busy = False                 # ★ 防击穿：同一时刻只允许一个线程真去抓


UP_MAX_PER_CH = 3                # 每个频道最多查几个候选


UP_PLACEHOLDER_DAYS = 7          # 开播时刻在 7 天以后 ⇒ 视为长期占位


def _up_candidates(handle):
    """该频道 /streams 页里带「配信予定」徽标的条目 → [{vid, title}]"""
    ok, html, _f = fetch("https://www.youtube.com/%s/streams" % handle, timeout=25)
    if not ok:
        return []
    out = []
    pos = [m.start() for m in re.finditer(r'"lockupViewModel"', html)]
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else min(len(html), p + 12000)
        seg = html[p:end]
        if "配信予定" not in seg and "プレミア公開予定" not in seg:
            continue
        vid = re.search(r'"contentId":"([\w-]{11})"', seg)
        ti = (re.search(r'"title":\{"content":"([^"]{3,200})"', seg)
              or re.search(r'"title":\{"runs":\[\{"text":"([^"]{3,200})"', seg))
        if vid:
            out.append({"vid": vid.group(1), "title": (ti.group(1).strip() if ti else "")})
        if len(out) >= UP_MAX_PER_CH:
            break
    return out


def _up_meta(vid):
    """innertube player → {ts:UTC秒, live:bool, status:str, title:str}；失败 None。"""
    import datetime as _dt
    ok, txt = _post_json("https://www.youtube.com/youtubei/v1/player",
                         {"context": {"client": {"clientName": "WEB",
                                                 "clientVersion": "2.20240101.00.00",
                                                 "hl": "ja", "gl": "JP"}},
                          "videoId": vid})
    if not ok:
        return None
    try:
        j = json.loads(txt)
        mf = (j.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        lbd = mf.get("liveBroadcastDetails") or {}
        ts = None
        st = lbd.get("startTimestamp")
        if st:
            d = _dt.datetime.fromisoformat(str(st).strip().replace("Z", "+00:00"))
            ts = d.astimezone(_dt.timezone.utc).timestamp()
        return {"ts": ts, "live": bool(lbd.get("isLiveNow")),
                "status": ((j.get("playabilityStatus") or {}).get("status") or ""),
                "title": ((mf.get("title") or {}).get("simpleText") or "").strip()}
    except Exception:
        return None


def _bj(ts):
    """UTC 秒 → 北京时间。当天/次日带「今天/明天」前缀；**跨年的一定带年份**（占位条目多是 2027/2028）。"""
    import datetime as _dt
    bj = _dt.timezone(_dt.timedelta(hours=8))
    d = _dt.datetime.fromtimestamp(ts, bj)
    n = _dt.datetime.now(bj)
    if d.year != n.year:
        return d.strftime("%Y-%m-%d %H:%M")
    delta = (d.date() - n.date()).days
    pre = {0: "今天 ", 1: "明天 ", 2: "后天 "}.get(delta, "")
    return pre + d.strftime("%m-%d %H:%M").lstrip("0").replace("-0", "-")


def get_upcoming(force=False):
    """全成员待機所预告（含长期占位，已标 placeholder）。"""
    global _up_busy
    now = time.time()
    if (not force) and _UP_CACHE["data"] is not None and (now - _UP_CACHE["ts"]) < UP_TTL:
        return _UP_CACHE["data"]
    if _up_busy:
        # ★ 别在别人正抓的时候直接回空列表 —— 前端会把空的读成"没有预告"（撒谎）。
        #   等一会儿拿它抓完的结果；等不到才回上次的缓存。
        for _ in range(40):
            time.sleep(0.5)
            if not _up_busy:
                break
            if _UP_CACHE["data"] is not None and (time.time() - _UP_CACHE["ts"]) < UP_TTL:
                break
        return _UP_CACHE["data"] or []
    _up_busy = True
    try:
        from concurrent.futures import ThreadPoolExecutor
        items = []
        for m in MEMBERS:
            cands = _up_candidates(m["handle"])
            if not cands:
                continue
            with ThreadPoolExecutor(max_workers=len(cands)) as ex:
                metas = list(ex.map(lambda c: _up_meta(c["vid"]), cands))
            for c, meta in zip(cands, metas):
                if not meta or not meta.get("ts"):
                    continue
                if meta.get("live"):          # 真在播 → 交给卡片红点，不放进预告栏
                    continue
                ts = float(meta["ts"])
                title = (meta.get("title") or c["title"] or "")[:140]
                items.append({
                    "member": m["jp"], "member_id": m["id"], "role": m["role"],
                    "title": title, "vid": c["vid"],
                    "url": "https://www.youtube.com/watch?v=%s" % c["vid"],
                    "start_ts": int(ts), "start_bj": _bj(ts),
                    "hours_until": round((ts - now) / 3600.0, 1),
                    "placeholder": (ts - now) > UP_PLACEHOLDER_DAYS * 86400,
                    "members_only": ("メン限" in title or "メンバー限定" in title),
                    "status": meta.get("status") or "",
                })
        items.sort(key=lambda x: x["start_ts"])
        _UP_CACHE["data"] = items
        _UP_CACHE["ts"] = time.time()
        return items
    finally:
        _up_busy = False


def humanize(iso):
    """ISO 时间 → 中文相对时间（刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前 / N 年前）"""
    if not iso:
        return ""
    try:
        import datetime as _dt
        t = _dt.datetime.strptime(iso.strip()[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        d = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds()
    except Exception:
        return ""
    if d < 0:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % max(1, int(d // 60))
    if d < 86400:
        return "%d 小时前" % int(d // 3600)
    if d < 86400 * 30:
        return "%d 天前" % int(d // 86400)
    if d < 86400 * 365:
        return "%d 个月前" % max(1, int(d // (86400 * 30)))
    return "%d 年前" % int(d // (86400 * 365))


_recents = {}


_recent_ts = {}          # ucid -> 上次**成功**抓到的时间（用来节流，别每 60 秒重抓一遍）


RECENT_TTL_SHORT = 600   # 10 分钟内直接用缓存，不打网络


RECENT_TTL_LONG = 1800   # RSS 挂了时，重源最多每 30 分钟试一次（重源贵：2 个列表页 + 若干 watch 页）


def _save_recents():
    try:
        with open(RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": _recents, "ts": _recent_ts}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _rss_once(ucid):
    """单次 RSS 尝试：成功返回 dict，失败返回 None（绝不抛）。"""
    ok, xml, _f = fetch("https://www.youtube.com/feeds/videos.xml?channel_id=%s" % ucid, timeout=12)
    if not ok or "<entry>" not in xml:
        return None
    from html import unescape
    m = re.search(r"<entry>(.*?)</entry>", xml, re.S)
    if not m:
        return None
    ent = m.group(1)
    tm = re.search(r"<title>(.*?)</title>", ent, re.S)
    pm = re.search(r"<published>(.*?)</published>", ent)
    lm = re.search(r'<link rel="alternate" href="([^"]+)"', ent)
    if not (tm and lm):
        return None
    return {
        "title": unescape(tm.group(1)).strip()[:120],
        "published": (pm.group(1) if pm else ""),   # ★ 存原始时间戳，不存算好的「N 小时前」
        "url": lm.group(1),
        "entries": len(re.findall(r"<entry>", xml)),
        "source": "rss",
    }


def _to_utc(s):
    """watch 页的 uploadDate 常带本地时区偏移（如 -07:00），统一折算成 UTC。

    ⚠️ humanize() 是按 UTC 解析的 —— 不折算会**差好几个时区**（看似小错，实际报错时间）。
    """
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        return s


def _tab_candidates(handle, tab, n):
    """抓 /<handle>/<tab> 页，取前 n 条候选 {vid,title}（lockupViewModel 结构，见 recent_of）"""
    ok, html, _f = fetch("https://www.youtube.com/%s/%s" % (handle, tab), timeout=20)
    if not ok:
        return []
    pos = [m.start() for m in re.finditer(r'"lockupViewModel"', html)]
    out = []
    for i, p in enumerate(pos[:n]):
        end = pos[i + 1] if i + 1 < len(pos) else min(len(html), p + 12000)
        seg = html[p:end]
        vid = re.search(r'"contentId":"([\w-]{11})"', seg)
        ti = (re.search(r'"title":\{"content":"([^"]{3,200})"', seg)
              or re.search(r'"title":\{"runs":\[\{"text":"([^"]{3,200})"', seg))
        if vid:
            out.append({"vid": vid.group(1),
                        "title": (ti.group(1).strip() if ti else "")})
    return out


def _watch_uploaddate(vid):
    """去条目自己的 watch 页读 uploadDate（外部真值）"""
    ok, html, _f = fetch("https://www.youtube.com/watch?v=%s" % vid, timeout=20)
    if not ok:
        return None
    m = (re.search(r'"uploadDate":"([^"]+)"', html)
         or re.search(r'itemprop="uploadDate" content="([^"]+)"', html))
    return _to_utc(m.group(1)) if m else None


def _post_json(url, obj, timeout=20):
    """POST JSON（同一套代理逻辑）。返回 (ok, text)。不抛。"""
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept-Language": "ja,en;q=0.8"})
    p = _proxy()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p} if p else {}))
    try:
        with op.open(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace")
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _video_meta(vid):
    """★ 拿单条视频的**外部真值**：(UTC 时间, 标题)。首选 innertube player，失败回落 watch 页。

    ★ 2026-10-14 实测：`POST https://www.youtube.com/youtubei/v1/player`
    body `{"context":{"client":{"clientName":"WEB","clientVersion":"2.20240101.00.00","hl":"ja","gl":"JP"}},"videoId":"<id>"}`
    —— **不需要 key**（带公开 web key 也一样），响应**只 ~23KB / 约 2 秒**，
    时间在 `microformat.playerMicroformatRenderer.publishDate`，**与 RSS 的 published 完全一致**。
    相比之下抓 watch 页要 ~1MB 且慢得多 ⇒ 要比较很多候选时一律走这条。
    """
    ok, txt = _post_json("https://www.youtube.com/youtubei/v1/player",
                         {"context": {"client": {"clientName": "WEB",
                                                 "clientVersion": "2.20240101.00.00",
                                                 "hl": "ja", "gl": "JP"}},
                          "videoId": vid})
    if ok:
        try:
            j = json.loads(txt)
            mf = (j.get("microformat") or {}).get("playerMicroformatRenderer") or {}
            d = mf.get("publishDate") or mf.get("uploadDate")
            ti = (mf.get("title") or {}).get("simpleText") or ""
            if d:
                return _to_utc(d), ti.strip()
        except Exception:
            pass
    d = _watch_uploaddate(vid)          # 回落（真值，但贵）
    return (d, "") if d else (None, "")


def _is_future(pub):
    """publishDate 落在未来 = 预告/待首播，**不能当成"最新上传"**（否则前端会显示「刚刚」骗人）。
    留 1 小时余量吸收时区误差。"""
    import datetime as _dt
    try:
        d = _dt.datetime.strptime(pub.strip()[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        return (d - _dt.datetime.now(_dt.timezone.utc)).total_seconds() > 3600
    except Exception:
        return False


def latest_upload_scrape(handle):
    """RSS 不可用时的替代源：候选 → 逐条读 publishDate → 取**真值最新**的那条。

    ★ 候选数不能太小：实测仲町あられ的 `/streams` **前 3 条是置顶的宣传/预告**
    （「ライブTY」「誕生日グッズ」「ライブ開催」），真实歌枠在第 4 条 ——
    正是 §4.5「排序会骗人」的实例。所以放宽到 streams 前 6 + videos 前 3。
    """
    cands = []
    for tab, n in (("streams", 6), ("videos", 3)):
        for c in _tab_candidates(handle, tab, n):
            if all(c["vid"] != x["vid"] for x in cands):
                cands.append(c)
    if not cands:
        return None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        metas = list(ex.map(lambda c: _video_meta(c["vid"]), cands))
    best = None                          # (候选, 时间, 标题)
    for c, (d, ti) in zip(cands, metas):
        if d and not _is_future(d) and (best is None or d > best[1]):
            best = (c, d, ti)
    if not best:
        return None
    return {"title": (best[2] or best[0]["title"])[:120], "published": best[1],
            "url": "https://www.youtube.com/watch?v=%s" % best[0]["vid"],
            "entries": 0, "source": "页面"}


def _fmt(ent, stale):
    return {"title": ent.get("title", ""), "when": humanize(ent.get("published", "")),
            "url": ent.get("url"), "count": ent.get("entries", 0),
            "source": ent.get("source", ""), "stale": stale}


def latest_video(ucid, handle=None):
    """★ 拿某频道**最新一个上传**（视频或直播档）。

    三级来源，逐级降级（2026-10-14 加的重源）：
      ① RSS（feeds/videos.xml）—— 最可靠，自带精确 published，天然倒序；但**端点会整段失效**
         （实测 2026-10-14：回源一律 404，第三方 r.jina.ai 代抓也是 Google 的 404 页，
          ⇒ 与本机/节点无关）。所以先重试 3 次。
      ② 页面重源（latest_upload_scrape）—— /streams + /videos 候选 → 各自的 uploadDate 比大小。
      ③ 上次成功值回退（stale=True，前端标 ※），**绝不把旧数据伪装成最新**。
    ★ 节流：10 分钟内直接用缓存不打网络；重源最多每 30 分钟试一次（它贵）。
    """
    if not ucid:
        return None
    now = time.time()
    ent = _recents.get(ucid)
    age = (now - _recent_ts.get(ucid, 0)) if ent else 1e9
    if ent and age < RECENT_TTL_SHORT:
        return _fmt(ent, False)          # 刚抓过，直接用

    got = None
    for wait in (0, 0.5, 1.4):           # ① RSS（失败是秒回，重试很便宜）
        if wait:
            time.sleep(wait)
        try:
            got = _rss_once(ucid)
        except Exception:
            got = None
        if got:
            break

    if not got and handle and age >= RECENT_TTL_LONG:   # ② 重源（贵，节流 30 分钟）
        try:
            got = latest_upload_scrape(handle)
        except Exception:
            got = None

    if got:
        with _lock:
            _recents[ucid] = got
            _recent_ts[ucid] = now
        _save_recents()
        return _fmt(got, False)
    if ent:                              # ③ 回退
        return _fmt(ent, True)
    return None


def build_status():
    """★ 并发查 5 个频道（串行时无代理要 5×超时，慢到不可用）"""
    from concurrent.futures import ThreadPoolExecutor

    def one(m):
        ucid, err = resolve_ucid(m)
        st = live_of(m) if ucid else None
        try:
            rc = latest_video(ucid, m["handle"])   # ★ 最新上传：RSS → 页面重源 → 上次成功值
        except Exception:
            rc = None
        return {
            "id": m["id"], "jp": m["jp"], "role": m["role"], "handle": m["handle"],
            "ucid": ucid, "live": (True if isinstance(st, str) else st),
            "videoId": (st if isinstance(st, str) else None),   # ★ 正在播时的视频 ID，前端据此自动投放
            "recent": rc,
            "note": (err if (not ucid) else ("代理未开或网络不可达" if st is None else "")),
            "url": "https://www.youtube.com/%s" % m["handle"],
        }

    with ThreadPoolExecutor(max_workers=len(MEMBERS)) as ex:
        return list(ex.map(one, MEMBERS))


def get_status(force=False):
    global _status_busy
    with _lock:
        if not force and _status_cache["data"] and (time.time() - _status_cache["ts"] < STATUS_TTL):
            return _status_cache["data"]

    # ★ 防"击穿"：缓存刚过期时 N 个人同时刷 → 会一起穿透去打 YouTube。
    #   已经有线程在刷就等它出结果，别重复打（同时最多 1 次真实抓取）。
    # 🔴🔴 2026-10-14 修 **死锁**：原来的等待循环是**写在 `with _lock:` 里面**的 ——
    #   等的人**抱着锁睡觉**，而 build_status 的 6 个线程写完结果时要拿同一把锁
    #   （latest_video 里 `with _lock: _recents[..] = ..`）⇒ **构建者永远拿不到锁、等者永远等不到数据**。
    #   实测症状：两个并发 /api/status?force=1 **双双超时 200s**；单个请求则表现为「等 30s 拿到空列表」。
    #   正解：**等待时不持锁**（下面这个循环只在极短时间内取锁读状态），且超时上限要**长于一次冷构建**。
    d = _status_cache["data"]
    deadline = time.time() + 150
    while True:
        with _lock:
            busy = _status_busy
            d = _status_cache["data"] or d
            fresh = bool(d) and (time.time() - _status_cache["ts"] < STATUS_TTL)
        if fresh or not busy:
            break
        if d:                      # 有旧的先给旧数据，别让前端白屏
            return d
        if time.time() > deadline:
            return _status_cache["data"] or []
        time.sleep(0.5)

    with _lock:
        # 二次检查：等的这段时间里别人可能已经刷完了
        if not force and _status_cache["data"] and (time.time() - _status_cache["ts"] < STATUS_TTL):
            return _status_cache["data"]
        if _status_busy:           # 别人刚开始刷 → 复用它的结果
            return _status_cache["data"] or []
        _status_busy = True
    try:
        data = build_status()
        with _lock:
            _status_cache.update({"ts": time.time(), "data": data})
        return data
    finally:
        with _lock:
            _status_busy = False

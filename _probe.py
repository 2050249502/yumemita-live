#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取探针 v2（跑在 GitHub runner 上，2026-10-17）

问题：本机靠机场节点能抓到 YouTube / X / B站，但 GitHub 的 runner 是**机房 IP**，
      平台对机房 IP 往往更严（验证码 / 429 / 风控）。

v1 教训：`get_x_schedule` 抓失败时返回 **None**，v1 把它记成 ok=True / n=0 ⇒ 假阳性。
        v2 把「返回 None」判为失败，并对 X 追加**原始诊断**（各 UA 下的状态码 / 正文大小 /
        特征词），这样「被挡」和「当天真没发配信表」能区分开。

报告写进 _probe_report.json，由 workflow 提交回仓库 —— Actions 日志匿名读不到（403）。
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def egress():
    out = {}
    for url in ("https://ipinfo.io/json", "https://api.ipify.org?format=json"):
        try:
            with urllib.request.urlopen(url, timeout=12) as r:
                body = r.read().decode("utf-8", "replace")
            try:
                out[url] = json.loads(body)
            except Exception:
                out[url] = body[:120]
        except Exception as e:
            out[url] = "ERR %s: %s" % (type(e).__name__, str(e)[:100])
    return out


def x_raw_diag():
    """对 X 直接发几种 UA 的裸请求，看机房 IP 到底被怎么对待。"""
    uas = [
        ("server.UA", server.UA),
        ("Safari/mac", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
        ("curl", "curl/8.4.0"),
    ]
    out = []
    for label, ua in uas:
        t0 = time.time()
        rec = {"ua": label}
        try:
            req = urllib.request.Request("https://x.com/%s" % server.X_USER,
                                         headers={"User-Agent": ua, "Accept-Language": "ja,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                rec["status"] = r.status
        except urllib.error.HTTPError as e:
            rec["status"] = e.code
            try:
                body = (e.read() or b"").decode("utf-8", "replace")
            except Exception:
                body = ""
        except Exception as e:
            rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:140])
            rec["secs"] = round(time.time() - t0, 1)
            out.append(rec)
            continue
        rec["bytes"] = len(body)
        rec["secs"] = round(time.time() - t0, 1)
        rec["marks"] = {m: True for m in ("full_text", "login", "Rate limit", "Something went wrong",
                                          "consent", "Enable JavaScript", "suspended", "配信スケジュール")
                        if m in body}
        out.append(rec)
    # 再走一遍项目自己的 fetch（同一段代码路径）
    try:
        ok, body, _ = server.fetch("https://x.com/%s" % server.X_USER, timeout=25)
        out.append({"ua": "server.fetch", "ok": ok, "bytes": len(body or ""),
                    "has_full_text": "full_text" in (body or "")})
    except Exception as e:
        out.append({"ua": "server.fetch", "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
    return out


def x_alt_diag():
    """给 X 找一条**机房 IP 走得通**的路：官方 x.com 首屏已被 403，试几个公开替代通道。

    判定标准：能拿到 200 且正文里出现「配信スケジュール」这类内容 ⇒ 这条路可用。
    """
    cands = [
        ("fxtwitter（JSON API）", "https://api.fxtwitter.com/%s" % server.X_USER),
        ("vxtwitter（JSON API）", "https://api.vxtwitter.com/%s" % server.X_USER),
        ("syndication 时间线（HTML 内嵌 JSON）",
         "https://syndication.twitter.com/srv/timeline-profile/screen-name/%s" % server.X_USER),
        ("publish.twitter.com oEmbed", "https://publish.twitter.com/oembed?url=https://twitter.com/%s" % server.X_USER),
    ]
    out = []
    for label, url in cands:
        t0 = time.time()
        rec = {"channel": label, "url": url[:70]}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": server.UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                rec["status"] = r.status
        except urllib.error.HTTPError as e:
            rec["status"] = e.code
            try:
                body = (e.read() or b"").decode("utf-8", "replace")
            except Exception:
                body = ""
        except Exception as e:
            rec["err"] = "%s: %s" % (type(e).__name__, str(e)[:110])
            rec["secs"] = round(time.time() - t0, 1)
            out.append(rec)
            continue
        rec["bytes"] = len(body)
        rec["secs"] = round(time.time() - t0, 1)
        rec["has_schedule_word"] = "配信スケジュール" in body
        rec["has_full_text"] = "full_text" in body
        rec["has_tweet_json"] = ('"tweets"' in body) or ("__NEXT_DATA__" in body)
        # 拿到 JSON 的，顺手看第一条推文长什么样
        try:
            j = json.loads(body)
            if isinstance(j, dict) and isinstance(j.get("tweets"), list) and j["tweets"]:
                rec["first_tweet_text"] = (j["tweets"][0].get("text") or "")[:80]
                rec["tweet_count"] = len(j["tweets"])
        except Exception:
            pass
        out.append(rec)
    return out


def x_fallback_diag():
    """v4 专项：给 X 找能用的云端通道（x.com 首屏在机房 IP 上一律 403）。

    逐个候选：状态码 / 正文大小 / 是否含 full_text / 是否含「配信スケジュール」。
    syndication 那条**带退避重试**（实测首击常 429）。
    """
    cands = [
        ("syndication 时间线", "https://syndication.twitter.com/srv/timeline-profile/screen-name/%s" % server.X_USER),
        ("r.jina.ai 读 x.com", "https://r.jina.ai/https://x.com/%s" % server.X_USER),
        ("r.jina.ai 读 syndication", "https://r.jina.ai/https://syndication.twitter.com/srv/timeline-profile/screen-name/%s" % server.X_USER),
        ("fxtwitter 最新推文", "https://api.fxtwitter.com/%s/status/latest" % server.X_USER),
    ]
    out = []
    for label, url in cands:
        rec = {"channel": label}
        for attempt in range(1, 3):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": server.UA,
                                                           "Accept-Language": "ja,en;q=0.8"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    body = r.read().decode("utf-8", "replace")
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
                try:
                    body = (e.read() or b"").decode("utf-8", "replace")
                except Exception:
                    body = ""
            except Exception as e:
                rec["attempt%d" % attempt] = "ERR %s: %s" % (type(e).__name__, str(e)[:90])
                time.sleep(min(3 * attempt, 5))
                continue
            rec["attempt%d" % attempt] = {"status": code, "bytes": len(body), "secs": round(time.time() - t0, 1),
                                          "full_text": body.count("full_text"),
                                          "配信スケジュール": body.count("配信スケジュール")}
            if code == 200 and "full_text" in body:
                rec["OK_AT_ATTEMPT"] = attempt
                break
            time.sleep(min(3 * attempt, 5))
        out.append(rec)
    return out


def x_forced_syndication():
    """强制走备用通道跑一次真实的 x_schedule()，看能否拿到配信表。"""
    os.environ["YUMEMITA_X_SOURCE"] = "syndication"
    t0 = time.time()
    try:
        d = server.x_schedule(3)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    finally:
        os.environ.pop("YUMEMITA_X_SOURCE", None)
    if not d:
        return {"ok": False, "secs": round(time.time() - t0, 1), "reason": "返回 None/空（抓不到）"}
    return {"ok": True, "secs": round(time.time() - t0, 1), "n": len(d),
            "entries": [{"date": e["date"], "slots": len(e["slots"]), "text_head": e["text"][:50]} for e in d]}


def digest(r):
    """把返回值压成 (条数, 样本, 首条键, 附加)。返回 None ⇒ 判失败（v1 的教训）。"""
    if r is None:
        return 0, None, "NoneType", {}
    if isinstance(r, list):
        items = r
    elif isinstance(r, dict):
        items = r.get("posts") or r.get("list") or r.get("members") or []
    else:
        return 0, None, type(r).__name__, {}
    n = len(items)
    first = items[0] if items else None
    sample, keys = None, None
    if isinstance(first, dict):
        keys = list(first.keys())[:10]
        for k in ("title", "text", "body", "name", "jp", "id"):
            v = first.get(k)
            if isinstance(v, str) and len(v) > 3:
                sample = v[:90]
                break
        if sample is None:
            for k, v in first.items():
                if isinstance(v, str) and len(v) > 8:
                    sample = "%s = %s" % (k, v[:70])
                    break
    extra = {}
    if isinstance(first, dict) and ("jp" in first or "handle" in first):
        extra["with_live"] = sum(1 for m in items if m.get("live"))
        extra["with_recent"] = sum(1 for m in items if m.get("recent"))
        m2 = next((m for m in items if (m.get("recent") or {}).get("title")), None)
        if m2:
            extra["recent_sample"] = "%s → %s" % (m2.get("jp"), m2["recent"]["title"][:60])
    if isinstance(first, dict) and "bvid" in first:
        extra["bvid_sample"] = first.get("bvid")
        extra["author_sample"] = first.get("author")
    return n, sample, keys, extra


rep = {
    "when_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    "probe_version": 41,
    "runner": os.environ.get("RUNNER_NAME") or "(unknown)",
    "python": sys.version.split()[0],
    "egress": egress(),
    "blocks": {},
    "x_raw": [],
    "x_alt": [],
    "x_fallback": [],
    "x_forced": {},
    "all_ok": True,
}

CHECKS = [
    ("status（YouTube 在播 + 每人最新视频）", server.get_status),
    ("x_schedule（官方 X 配信表）", server.get_x_schedule),
    ("upcoming（YouTube 待機所）", server.get_upcoming),
    ("bilibili（B站 搬运，含白名单过滤）", server.get_bilibili),
]

for name, fn in CHECKS:
    t0 = time.time()
    rec = {}
    try:
        r = fn(True)
        n, sample, keys, extra = digest(r)
        rec["ok"] = r is not None            # ★ v2：None 判失败
        rec["secs"] = round(time.time() - t0, 1)
        rec["returned_none"] = r is None
        rec["n"] = n
        rec["first_keys"] = keys
        rec["sample"] = sample
        rec.update(extra)
        if r is None:
            rec["error"] = "抓取函数返回 None（fetch 失败或解析不到）"
            rep["all_ok"] = False
        elif n == 0:
            rec["warn"] = "返回空列表（可能是当天确实没内容，也可能是页面被挡/改版）"
    except Exception as e:
        rec["ok"] = False
        rec["secs"] = round(time.time() - t0, 1)
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:220])
        rec["trace_tail"] = traceback.format_exc()[-500:]
        rep["all_ok"] = False
    rep["blocks"][name] = rec
    print("[probe] %-40s ok=%s %ss n=%s %s" % (name, rec["ok"], rec.get("secs"), rec.get("n"),
                                               rec.get("error") or (rec.get("sample") or "")[:40]), flush=True)

rep["x_raw"] = x_raw_diag()
rep["x_fallback"] = x_fallback_diag()
rep["x_forced"] = x_forced_syndication()
print("[probe] 备用通道:", json.dumps(rep["x_fallback"], ensure_ascii=False)[:600], flush=True)
print("[probe] 强制备用通道跑 x_schedule:", json.dumps(rep["x_forced"], ensure_ascii=False)[:400], flush=True)
print("[probe] X 原始诊断:", json.dumps(rep["x_raw"], ensure_ascii=False)[:300], flush=True)
rep["x_alt"] = x_alt_diag()
print("[probe] X 备选通道:", json.dumps(rep["x_alt"], ensure_ascii=False)[:700], flush=True)

rep["report_readme"] = ("探针报告：all_ok=true = 四个数据源全部抓到真数据（返回 None 或抛异常都算失败）；"
                        "某块 n=0 且有 warn ⇒ 看 x_raw / first_keys 判断是被挡还是真没内容。")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_report.json"), "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print("\n[probe] all_ok =", rep["all_ok"])
sys.exit(0 if rep["all_ok"] else 3)

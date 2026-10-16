#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取探针（跑在 GitHub runner 上，2026-10-17）

问题：本机靠机场节点能抓到 YouTube / X / B站，但 GitHub 的 runner 是**机房 IP**，
      平台对机房 IP 往往更严（验证码 / 429 / 风控）。这个脚本就是来问这个问题的。

做法：直接调用抓取模块的四个入口，逐块记耗时、条数、**首条真数据样本**，
      写成 _probe_report.json；再由 workflow 提交回仓库 ——
      Actions 日志匿名读不到（403），报告文件才是可读的凭据。
"""
import json
import os
import sys
import time
import traceback
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def egress():
    """本 runner 的出口 IP / 地区（证明它在不在墙内、在哪）。"""
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


def digest(r):
    """把任意返回值压成 (条数, 样本, 首条键, 附加统计) —— 四个入口返回的都是 list。"""
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
    "runner": os.environ.get("RUNNER_NAME") or "(unknown)",
    "python": sys.version.split()[0],
    "egress": egress(),
    "blocks": {},
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
        rec["ok"] = True
        rec["secs"] = round(time.time() - t0, 1)
        n, sample, keys, extra = digest(r)
        rec["n"] = n
        rec["first_keys"] = keys
        rec["sample"] = sample
        rec.update(extra)
    except Exception as e:
        rec["ok"] = False
        rec["secs"] = round(time.time() - t0, 1)
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:220])
        rec["trace_tail"] = traceback.format_exc()[-500:]
        rep["all_ok"] = False
    rep["blocks"][name] = rec
    print("[probe] %-40s ok=%s %ss n=%s %s" % (name, rec["ok"], rec.get("secs"), rec.get("n"),
                                               rec.get("error") or (rec.get("sample") or "")[:40]), flush=True)

rep["report_readme"] = ("这是探针报告：all_ok=true 表示 GitHub runner 这一侧四个数据源全部抓到真数据；"
                        "false 则看 blocks.<块>.error / trace_tail。")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_report.json"), "w", encoding="utf-8") as f:
    json.dump(rep, f, ensure_ascii=False, indent=2)
print("\n[probe] all_ok =", rep["all_ok"])
print(json.dumps(rep, ensure_ascii=False)[:1400])
sys.exit(0 if rep["all_ok"] else 3)

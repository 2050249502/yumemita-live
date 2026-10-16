#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""梦限大直播站 · 静态版数据生成器（2026-10-17）

把原来「backends 现算」的四块数据抓一次、落成 site/data.json：
    members   ← server.get_status()      （谁在播 + videoId + 每人最新视频）
    schedule  ← server.get_x_schedule()  （官方 X 配信表）
    upcoming  ← server.get_upcoming()    （各成员 YouTube 待機所预告）
    bilibili  ← server.get_bilibili()    （B站搬运）
    notice    ← notice.txt 全文
页面端只读这一个文件，不需要任何后端进程。

设计要点：
  · **分块兜底**：某一块抓失败时，沿用上一份 data.json 里的旧值（并记进 data.json["errors"]），
    整块面板不会因为一次网络抖动而空掉。
  · 写盘用 tmp + os.replace（原子替换），避免页面读到半个 JSON。
  · 由 Windows 计划任务每 5 分钟跑一次；跑完由 push_site 推给托管方。

用法：
    python build_data.py            # 抓一次并写 site/data.json
    python build_data.py --dry      # 只打印，不写盘
"""
import json
import os
import sys
import time
import traceback

# ★ Windows 计划任务把输出重定向到文件时用的是 GBK 代码页，非 ASCII 会直接抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.abspath(__file__))
# ★ 允许用环境变量指定发布目录（云端仓库把 index.html 放在仓库根 ⇒ YUMEMITA_SITE_DIR=. ）
SITE = os.path.abspath(os.environ.get("YUMEMITA_SITE_DIR") or os.path.join(ROOT, "site"))
OUT = os.path.join(SITE, "data.json")
NOTICE = os.path.join(SITE, "notice.txt")
DRY = "--dry" in sys.argv


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    t0 = time.time()
    import server as S                      # 复用现有抓取逻辑（判活/RSS/X/B站）

    prev = load_prev()
    data = {"ts": int(time.time()), "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "errors": {}, "source": "build_data.py"}

    def sect(name, fn, key=None):
        try:
            v = fn(True)
            if key:
                data[key] = v
            else:
                data[name] = v
            n = len(v) if isinstance(v, (list, dict)) else "-"
            log("  ok   %-9s %s" % (name, n))
        except Exception as e:
            data["errors"][name] = str(e)[:200]
            if key and prev.get(key) is not None:
                data[key] = prev[key]
                log("  !!   %-9s 失败：%s ⇒ 沿用上一份" % (name, str(e)[:80]))
            else:
                data[key or name] = ([] if name != "bilibili" else {"list": []})
                log("  !!   %-9s 失败且没有旧值：%s" % (name, str(e)[:80]))
            if "--trace" in sys.argv:
                traceback.print_exc()

    sect("members", S.get_status)

    # ★ 前端要按**原始时间**现算「X 分钟前」（快照里的 when 是生成那一刻的字符串，放久了会一直说"37 分钟前"）
    #   ⇒ 把 recent_cache 里的 published 一并带进 data.json
    try:
        recents = getattr(S, "_recents", {}) or {}
        got = 0
        for m in (data.get("members") or []):
            r = m.get("recent") or {}
            pub = (recents.get(m.get("ucid") or "") or {}).get("published")
            if pub:
                r["published"] = pub
                m["recent"] = r
                got += 1
        log("  ok   published  %d 人的最新视频带上原始时间" % got)
    except Exception as e:
        log("  !!   published 注入失败：%s" % e)

    sect("schedule", (lambda f: {"posts": S.get_x_schedule(f)}), key="schedule")
    sect("upcoming", S.get_upcoming)
    sect("bilibili", (lambda f: {"list": S.get_bilibili(f)}), key="bilibili")

    # 公告：直接读 site/notice.txt（纯静态文件，改它就是改公告）
    try:
        with open(NOTICE, encoding="utf-8") as f:
            data["notice"] = f.read().strip()
        log("  ok   notice     %d 字" % len(data["notice"]))
    except Exception as e:
        data["notice"] = prev.get("notice", "")
        data["errors"]["notice"] = str(e)[:200]
        log("  !!   notice 失败：%s" % e)

    if not DRY:
        os.makedirs(SITE, exist_ok=True)
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, OUT)

    live = [m["jp"] for m in (data.get("members") or []) if m.get("live")]
    log("完成：在播=%s，用时 %.1fs，data.json=%.1f KB%s"
        % (live or "无", time.time() - t0,
           (os.path.getsize(OUT) / 1024) if os.path.exists(OUT) else 0,
           "（--dry 没写盘）" if DRY else ""))
    if data["errors"]:
        log("注意：这些块失败了 → %s" % ", ".join(data["errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

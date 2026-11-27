#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
入口站 · 手动探测（本地版）—— 与青龙 monitor_snapshot.py 的 run() 逐条对齐
==========================================================================
诊断用：在本地把 11 项探测原样跑一遍。
红线：**判据必须与 scripts/monitor_snapshot.py 的 run() 一致**，否则手动数据进历史后
      与定时数据不可比。若那边改了 PAGES / 路径 / 判据，本文件同步改。
      页面端 index.html 里的同名逻辑也遵循同一口径（三处同源）。

用法：
    python3 manual_probe.py           # 人读
    python3 manual_probe.py --json    # JSON（字段与页面导出、latest.json 一致）
"""

import sys
import json
import time
import datetime
import urllib.request
import urllib.error

TIMEOUT = 15
UA = "Mozilla/5.0 (compatible; hub-manual-probe/1.0)"

ADDR = {
    "相册展示": "https://elizond0.github.io/album/base.json",
    "PDF工具": "https://elizond0.github.io/pdf-tools/nas.json",
}
PAGES = {
    "相册展示-前端": "https://elizond0.github.io/album/",
    "PDF工具-前端": "https://elizond0.github.io/pdf-tools/",
    "什么值得买-入口": "https://elizond0.github.io/smzdm-daily/",
    "哥俩好小游戏-入口": "https://elizond0.github.io/gelianghao/",
    "回形针游戏-入口": "https://elizond0.github.io/paperclip-max/",
}


def http(url, method="GET", headers=None, timeout=TIMEOUT):
    """返回 (status, body_bytes, headers_dict)；失败 status=0。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(65536), {k: v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        try:
            b = e.read(65536)
        except Exception:
            b = b""
        return e.code, b, {k: v for k, v in (e.headers or {}).items()}
    except Exception:
        return 0, b"", {}


def get_addr_file(tool):
    """与 monitor_snapshot.py 的 get_addr_file 同判据。"""
    url = ADDR[tool]
    st, b, _ = http(url + "?t=%d" % int(time.time() * 1000))
    if st != 200:
        raise RuntimeError("HTTP %d" % st)
    d = json.loads(b.decode("utf-8", "ignore"))
    addr = (d.get("base") or d.get("url") or "").strip()
    if not addr:
        raise RuntimeError("地址字段为空")
    return addr, "地址=" + addr


def run():
    results = []
    addr_map = {}

    # ---- 地址文件 2 项 ----
    for tool in ADDR:
        try:
            addr, detail = get_addr_file(tool)
            addr_map[tool] = addr
            results.append({"item": tool + "-地址文件(base/nas.json)", "ok": True, "detail": detail})
        except Exception as e:
            results.append({"item": tool + "-地址文件(base/nas.json)", "ok": False, "detail": str(e)[:80]})

    # ---- 相册 2 项 ----
    base = addr_map.get("相册展示")
    if base:
        try:
            st, b, hd = http(base + "/api/health")
            body = b.decode("utf-8", "ignore")
            ok = st == 200 and '"ok":true' in body.replace(" ", "")
            results.append({"item": "相册展示-隧道 /api/health", "ok": ok,
                            "detail": "HTTP %d body=%s ACAO=%s" % (st, body.strip()[:40],
                                                                   hd.get("Access-Control-Allow-Origin", ""))})
        except Exception as e:
            results.append({"item": "相册展示-隧道 /api/health", "ok": False, "detail": str(e)[:80]})
        try:
            st, b, hd = http(base + "/index.json")
            cc = hd.get("Cache-Control", "")
            results.append({"item": "相册展示-隧道 index.json 缓存头", "ok": st == 200 and cc == "no-store",
                            "detail": "HTTP %d cache-control=%s" % (st, cc)})
        except Exception as e:
            results.append({"item": "相册展示-隧道 index.json 缓存头", "ok": False, "detail": str(e)[:80]})
    else:
        for n in ("相册展示-隧道 /api/health", "相册展示-隧道 index.json 缓存头"):
            results.append({"item": n, "ok": False, "detail": "地址文件失败，无法探测"})

    # ---- PDF 2 项 ----
    url = addr_map.get("PDF工具")
    if url:
        try:
            st, b, hd = http(url + "/api/v1/info/status")
            results.append({"item": "PDF工具-隧道门禁(无key)", "ok": st == 403, "detail": "HTTP %d" % st})
        except Exception as e:
            results.append({"item": "PDF工具-隧道门禁(无key)", "ok": False, "detail": str(e)[:80]})
        try:
            st, b, hd = http(url + "/api/v1/misc/compress-pdf", method="OPTIONS")
            results.append({"item": "PDF工具-隧道预检(OPTIONS)", "ok": st == 204,
                            "detail": "HTTP %d ACAO=%s" % (st, hd.get("Access-Control-Allow-Origin", ""))})
        except Exception as e:
            results.append({"item": "PDF工具-隧道预检(OPTIONS)", "ok": False, "detail": str(e)[:80]})
    else:
        for n in ("PDF工具-隧道门禁(无key)", "PDF工具-隧道预检(OPTIONS)"):
            results.append({"item": n, "ok": False, "detail": "地址文件失败，无法探测"})

    # ---- 5 个前端 ----
    for name, u in PAGES.items():
        try:
            st, b, hd = http(u)
            results.append({"item": name, "ok": st == 200, "detail": "HTTP %d" % st})
        except Exception as e:
            results.append({"item": name, "ok": False, "detail": str(e)[:80]})

    total = len(results)
    good = sum(1 for r in results if r["ok"])
    return {"date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": "%d/%d" % (good, total), "results": results, "source": "manual"}


def main():
    p = run()
    if "--json" in sys.argv:
        print(json.dumps(p, ensure_ascii=False, indent=2))
    else:
        print("手动探测  %s   %s" % (p["date"], p["summary"]))
        print("-" * 74)
        for r in p["results"]:
            print("%s  %-36s %s" % ("✓" if r["ok"] else "✗", r["item"], r["detail"]))
        print("-" * 74)
        print("异常 %d 项" % sum(1 for r in p["results"] if not r["ok"]))
    return 0 if p["summary"].split("/")[0] == p["summary"].split("/")[1] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自制工具 · 每日服务监测（公网侧轻量版）
=====================================================================
设计原则（来自 nas工具部署指引 / 各工具部署必读的实证）：
  1. 只用公网可达的信号，不登录 NAS、不碰任何凭据 —— 因此可在任意对话/定时任务里跑。
  2. 隧道地址永远现读 GitHub 上的 base.json / nas.json，不写死 trycloudflare 域名。
  3. 判活看"实际探测隧道端点"，不看地址文件的 updated 时间 —— 时间新≠隧道活、时间旧≠隧道死。
  4. 每个检查项给出可量判据与失败原因，不给出模糊的"疑似正常"。

监测对象（验证码工具不纳入：纯工具、无服务、无隧道）：
  - 相册展示 : Pages 前端 + 隧道 /api/health + index.json 的 no-store
  - pdf 工具 : Pages 前端 + 隧道门禁 403/204（带 key 则为 200）
  - 静态站点 : 什么值得买 / 哥俩好 —— 仅作"工具入口展示"，检查 Pages 200
  - 签到     : 云端任务 15581633 由本任务单独核对 last_run；NAS 端青龙需登录，另记

用法：
  python3 monitor.py            # 输出简洁文本报告到 stdout，并写 reports/<日期>.md
  python3 monitor.py --json     # 同时输出机器可读 JSON
退出码：0 全部正常 / 1 有服务异常 / 2 脚本自身异常
"""
import sys, json, os, datetime, urllib.request, urllib.error, ssl

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
os.makedirs(REPORTS, exist_ok=True)

TIMEOUT = 20
CTX = ssl.create_default_context()  # 校验证书；trycloudflare 为合法证书

# ---------- 监测清单：新增工具按此格式加一行即可纳入 ----------
PAGES = {
    "相册展示-前端": "https://elizond0.github.io/album/",
    "PDF工具-前端": "https://elizond0.github.io/pdf-tools/",
    "什么值得买-入口": "https://elizond0.github.io/smzdm-daily/",
    "哥俩好小游戏-入口": "https://elizond0.github.io/gelianghao/",
}
ADDR_FILES = {
    "相册展示": "https://raw.githubusercontent.com/elizond0/album/main/base.json",
    "PDF工具": "https://raw.githubusercontent.com/elizond0/pdf-tools/main/nas.json",
}
# 地址文件兜底：raw.githubusercontent.com 偶发整体超时（2026-11-23 实测连续 30s 超时），
# 改走各工具 Pages 站点的同名文件（更快、同源），二者内容一致。
ADDR_FALLBACK = {
    "相册展示": "https://elizond0.github.io/album/base.json",
    "PDF工具": "https://elizond0.github.io/pdf-tools/nas.json",
}
ORIGIN = "https://elizond0.github.io"


def fetch(url, method="GET", headers=None, timeout=TIMEOUT):
    """返回 (status, headers_dict, body_bytes, err)。任何异常都不抛出，转为 err。"""
    hs = {"User-Agent": "nas-monitor/1.0", "Accept": "*/*"}
    if headers:
        hs.update(headers)
    req = urllib.request.Request(url, data=None, method=method, headers=hs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, dict(r.headers), r.read(), None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), None
    except Exception as e:
        return None, {}, b"", f"{type(e).__name__}: {e}"


def header(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def age_hours(ts):
    """解析 ISO8601 UTC 时间，返回距今小时数；失败返回 None。"""
    if not ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600, 1)
    except Exception:
        return None


def check_pages():
    out = []
    for name, url in PAGES.items():
        st, hd, body, err = fetch(url)
        ok = st == 200
        out.append({
            "item": name, "kind": "静态页/入口", "ok": ok,
            "detail": f"HTTP {st}" + (f"  {err}" if err else "") + ("" if ok else "（期望 200）"),
        })
    return out


def check_album(base):
    """相册：/api/health 返回 ok + 单值 CORS；index.json 必须 no-store。"""
    out = []
    st, hd, body, err = fetch(base + "/api/health", headers={"Origin": ORIGIN})
    ok = (st == 200 and b'"ok"' in body and b"true" in body)
    acao = header(hd, "access-control-allow-origin")
    cors_ok = (acao is not None and "," not in acao)
    out.append({
        "item": "相册展示-隧道 /api/health", "kind": "隧道穿透", "ok": ok and cors_ok,
        "detail": (f"HTTP {st} body={body[:40].decode('utf-8','replace')} "
                   f"ACAO={acao!r}" + (f"  {err}" if err else "")
                   + ("" if ok and cors_ok else "（期望 200 + {\"ok\":true} + CORS 单值）")),
    })
    st, hd, body, err = fetch(base + "/index.json")
    cc = header(hd, "cache-control")
    ok = st == 200 and cc is not None and "no-store" in cc
    out.append({
        "item": "相册展示-隧道 index.json 缓存头", "kind": "隧道穿透", "ok": ok,
        "detail": f"HTTP {st} cache-control={cc!r}" + (f"  {err}" if err else "")
                  + ("" if ok else "（期望 200 + no-store）"),
    })
    return out


def check_pdf(url):
    """PDF：无 key 门禁 403、OPTIONS 预检 204；带 key 200（key 不落脚本，见《凭据》）。"""
    out = []
    st, hd, body, err = fetch(url + "/api/v1/info/status", headers={"Origin": ORIGIN})
    ok = st == 403
    out.append({
        "item": "PDF工具-隧道门禁(无key)", "kind": "隧道穿透", "ok": ok,
        "detail": f"HTTP {st}" + (f"  {err}" if err else "") + ("" if ok else "（期望 403）"),
    })
    st, hd, body, err = fetch(url + "/api/v1/misc/compress-pdf", method="OPTIONS",
                              headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    ok = st == 204
    out.append({
        "item": "PDF工具-隧道预检(OPTIONS)", "kind": "隧道穿透", "ok": ok,
        "detail": f"HTTP {st} ACAO={header(hd,'access-control-allow-origin')!r}"
                  + (f"  {err}" if err else "") + ("" if ok else "（期望 204）"),
    })
    return out


def run():
    results, addr_info = [], {}
    results += check_pages()

    # 隧道地址：现读 GitHub，不写死域名
    for name, url in ADDR_FILES.items():
        st, hd, body, err = fetch(url, headers={"Cache-Control": "no-cache"})
        # raw 通道失败（超时/非 200）时切 Pages 兜底，避免把网络抖动误报成工具故障
        if not body or st != 200:
            fb = ADDR_FALLBACK.get(name)
            if fb:
                st2, hd2, body2, err2 = fetch(fb, headers={"Cache-Control": "no-cache"})
                if body2 and st2 == 200:
                    body, err = body2, (err + " [raw失败,已用Pages兜底]").strip()
        rec = {"ok": False, "url": None, "updated": None, "age_h": None, "err": err}
        try:
            data = json.loads(body.decode("utf-8"))
            raw = data.get("base") or data.get("url")
            rec["url"] = str(raw).rstrip("/") if raw else None
            rec["updated"] = data.get("updated")
            rec["age_h"] = age_hours(rec["updated"])
            rec["ok"] = bool(rec["url"]) and rec["url"].startswith("https://")
        except Exception as e:
            rec["err"] = (rec["err"] or "") + f" 解析失败: {e}"
        addr_info[name] = rec
        results.append({
            "item": f"{name}-地址文件(base/nas.json)", "kind": "地址自愈", "ok": rec["ok"],
            "detail": (f"地址={rec['url']} updated={rec['updated']} 距今={rec['age_h']}h"
                       + (f"  {rec['err']}" if rec["err"] else "")
                       + ("" if rec["ok"] else "（期望读到有效 https 地址）")),
        })
        if rec["ok"]:
            if name == "相册展示":
                results += check_album(rec["url"])
            else:
                results += check_pdf(rec["url"])

    bad = [r for r in results if not r["ok"]]
    return {"results": results, "addr": addr_info, "bad": bad,
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def render(rep):
    icon = lambda ok: "✅" if ok else "❌"
    lines = ["# 自制工具 · 每日服务监测", "", f"生成时间：{rep['generated']}（北京时间）", ""]
    lines.append(f"**结论：{len(rep['results'])-len(rep['bad'])}/{len(rep['results'])} 项正常**"
                 + ("；⚠️ 存在异常，需处理" if rep["bad"] else "；全部通过"))
    lines += ["", "| 项 | 类别 | 结果 | 判据 / 实测 |", "|---|---|---|---|"]
    for r in rep["results"]:
        lines.append(f"| {r['item']} | {r['kind']} | {icon(r['ok'])} | {r['detail']} |")
    if rep["bad"]:
        lines += ["", "## 异常处理提示", ""]
        for r in rep["bad"]:
            lines.append(f"- **{r['item']}**：{r['detail']}")
        lines += ["", "> 隧道类异常先读对应部署必读的「按症状查 / 坑 22」：域名失效看 "
                  "cf.log 是否新增 `Tunnel not found`、watchdog.log 是否自愈；"
                  "地址文件时间仅供参考，不要据此判活。"]
    lines += ["", "---", "说明：签到云端任务(`15581633`)是否当日执行，由定时任务单独核对 `last_run_at`；"
              "NAS 端青龙需登录门户，本公网脚本不覆盖。验证码为纯工具，不纳入监测。"]
    return "\n".join(lines)


def main():
    try:
        rep = run()
    except Exception as e:
        print(f"脚本自身异常：{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    text = render(rep)
    print(text)
    day = datetime.date.today().isoformat()
    with open(os.path.join(REPORTS, day + ".md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    if "--json" in sys.argv:
        print("\n---JSON---")
        print(json.dumps({"generated": rep["generated"], "checks": rep["results"],
                          "addr": rep["addr"]}, ensure_ascii=False, indent=2))
    return 1 if rep["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具入口站 · 监测快照自动刷新（青龙版，每 8 小时）
=====================================================================
公网只读探测 10 项 + 签到 NAS 端核对 1 项 → 生成 latest.json → PUT 到 GitHub。

设计原则（对齐云端 monitor.py 与《nas工具部署指引》§11）：
  1. 只读探测，不登录 NAS、不改服务、不碰隧道配置。
  2. 隧道地址现读 GitHub 上的 base.json / nas.json，不写死 trycloudflare 域名。
  3. 判活看实际端点，不看地址文件 updated 时间。
  4. 唯一写操作：把快照 PUT 到 GitHub 仓库 tools-hub 的 data/latest.json。
     PAT 从环境变量 GH_PAT_TOOLSHUB 读取，不写死在脚本里、不打印。

环境变量（青龙「环境变量」里配置，脚本本身不含任何密钥）：
  GH_PAT_TOOLSHUB  GitHub PAT（需对 elizond0/tools-hub 有 push 权限）

分母口径：固定 10 项 = 地址文件 2 + 端点 8；签到 NAS 端当天执行则另记 1 项（说明列），
不进 results 数组（保持入口站兼容），只在 stdout 日志体现。验证码工具永不纳入。

退出码：0 正常 / 1 探测到异常 / 2 脚本自身异常（含缺密钥、写 GitHub 失败）
"""
import sys, json, os, datetime, urllib.request, urllib.error, ssl

CTX = ssl.create_default_context()
TIMEOUT = 20
UA = {'User-Agent': 'tools-hub-monitor/1.0'}

ADDR = {
    '相册展示': ('https://raw.githubusercontent.com/elizond0/album/main/base.json',
               'https://elizond0.github.io/album/base.json', 'base'),
    'PDF工具': ('https://raw.githubusercontent.com/elizond0/pdf-tools/main/nas.json',
              'https://elizond0.github.io/pdf-tools/nas.json', 'url'),
}
PAGES = {
    '相册展示-前端': 'https://elizond0.github.io/album/',
    'PDF工具-前端': 'https://elizond0.github.io/pdf-tools/',
    '什么值得买-入口': 'https://elizond0.github.io/smzdm-daily/',
    '哥俩好小游戏-入口': 'https://elizond0.github.io/gelianghao/',
}
NAS_KEY = 'pk_38b17a8712c561e6cd80b9d1'
REPO = 'elizond0/tools-hub'
PATH = 'data/latest.json'


def http(url, method='GET', headers=None, body=None, timeout=TIMEOUT):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        # 4xx/5xx 是判据信号（如门禁 403、OPTIONS 需另判），不当作网络异常
        return e.code, e.read(), dict(e.headers)


def get_addr_file(tool):
    """现读地址文件：raw 为主、Pages 兜底。返回 (地址, 说明)。"""
    raw, fallback, key = ADDR[tool]
    errs = []
    for src, url in (('raw', raw), ('pages', fallback)):
        try:
            st, b, _ = http(url + '?t=' + str(datetime.datetime.now().timestamp()))
            if st == 200:
                j = json.loads(b.decode('utf-8'))
                addr = (j.get(key) or '').rstrip('/')
                if addr:
                    return addr, '地址=' + addr
        except Exception as e:
            errs.append(src + ':' + type(e).__name__)
    raise RuntimeError('地址文件读取失败 ' + '/'.join(errs))


def run():
    results = []
    addr_map = {}
    # ---- 地址文件 2 项 ----
    for tool in ADDR:
        try:
            addr, detail = get_addr_file(tool)
            addr_map[tool] = addr
            results.append({'item': tool + '-地址文件(base/nas.json)', 'ok': True, 'detail': detail})
        except Exception as e:
            results.append({'item': tool + '-地址文件(base/nas.json)', 'ok': False, 'detail': str(e)})

    # ---- 端点 8 项 ----
    # 相册 /api/health
    base = addr_map.get('相册展示')
    if base:
        try:
            st, b, hd = http(base + '/api/health', timeout=TIMEOUT)
            body_txt = b.decode('utf-8', 'ignore').replace(' ', '')
            ok = st == 200 and '"ok":true' in body_txt
            results.append({'item': '相册展示-隧道 /api/health', 'ok': ok,
                            'detail': 'HTTP %d body=%s ACAO=%s' % (
                                st, b.decode('utf-8', 'ignore').strip()[:40], hd.get('Access-Control-Allow-Origin', ''))})
        except Exception as e:
            results.append({'item': '相册展示-隧道 /api/health', 'ok': False, 'detail': type(e).__name__ + ': ' + str(e)[:60]})
        # 相册 /index.json
        try:
            st, b, hd = http(base + '/index.json', timeout=TIMEOUT)
            ok = st == 200 and hd.get('Cache-Control', '') == 'no-store'
            results.append({'item': '相册展示-隧道 index.json 缓存头', 'ok': ok,
                            'detail': 'HTTP %d cache-control=%s' % (st, hd.get('Cache-Control', ''))})
        except Exception as e:
            results.append({'item': '相册展示-隧道 index.json 缓存头', 'ok': False, 'detail': type(e).__name__ + ': ' + str(e)[:60]})
    else:
        for name in ('相册展示-隧道 /api/health', '相册展示-隧道 index.json 缓存头'):
            results.append({'item': name, 'ok': False, 'detail': '地址文件失败，无法探测'})

    # PDF 门禁 403 + OPTIONS 204
    url = addr_map.get('PDF工具')
    if url:
        try:
            st, b, hd = http(url + '/api/v1/info/status', timeout=TIMEOUT)
            results.append({'item': 'PDF工具-隧道门禁(无key)', 'ok': st == 403, 'detail': 'HTTP %d' % st})
        except Exception as e:
            results.append({'item': 'PDF工具-隧道门禁(无key)', 'ok': False, 'detail': type(e).__name__ + ': ' + str(e)[:60]})
        try:
            st, b, hd = http(url + '/api/v1/misc/compress-pdf', method='OPTIONS', timeout=TIMEOUT)
            results.append({'item': 'PDF工具-隧道预检(OPTIONS)', 'ok': st == 204,
                            'detail': 'HTTP %d ACAO=%s' % (st, hd.get('Access-Control-Allow-Origin', ''))})
        except Exception as e:
            results.append({'item': 'PDF工具-隧道预检(OPTIONS)', 'ok': False, 'detail': type(e).__name__ + ': ' + str(e)[:60]})
    else:
        for name in ('PDF工具-隧道门禁(无key)', 'PDF工具-隧道预检(OPTIONS)'):
            results.append({'item': name, 'ok': False, 'detail': '地址文件失败，无法探测'})

    # 四个 Pages 前端
    for name, u in PAGES.items():
        try:
            st, b, hd = http(u, timeout=TIMEOUT)
            results.append({'item': name, 'ok': st == 200, 'detail': 'HTTP %d' % st})
        except Exception as e:
            results.append({'item': name, 'ok': False, 'detail': type(e).__name__ + ': ' + str(e)[:60]})

    return results


def checkin_nas_today():
    """核对 NAS 端青龙签到任务当天是否执行：看当天日志文件是否存在。不占分母。"""
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    logdir = '/ql/data/logs/scripts_checkin_v2'
    try:
        for f in os.listdir(logdir):
            if f.startswith(today) and f.endswith('.log'):
                return True, f
    except Exception as e:
        return False, type(e).__name__
    return False, '无当天日志'


def push_github(payload):
    """把快照 PUT 到 GitHub。PAT 从环境变量读，不打印。"""
    pat = os.environ.get('GH_PAT_TOOLSHUB', '').strip()
    if not pat:
        raise RuntimeError('缺环境变量 GH_PAT_TOOLSHUB')
    api = 'https://api.github.com/repos/%s/contents/%s' % (REPO, PATH)
    h = {'Authorization': 'Bearer ' + pat, 'Accept': 'application/vnd.github+json',
         'User-Agent': 'tools-hub-monitor/1.0'}
    # 先取 sha
    st, b, _ = http(api, headers=h, timeout=TIMEOUT)
    sha = ''
    if st == 200:
        sha = json.loads(b.decode('utf-8')).get('sha', '')
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')).decode()
    body = json.dumps({'message': 'monitor: auto snapshot %s' % payload['date'],
                       'content': content, 'sha': sha}).encode('utf-8')
    st, b, _ = http(api, method='PUT', headers=h, body=body, timeout=TIMEOUT)
    if st not in (200, 201):
        raise RuntimeError('GitHub PUT 失败 HTTP %d: %s' % (st, b.decode('utf-8', 'ignore')[:120]))
    return True


import base64

def main():
    try:
        results = run()
    except Exception as e:
        print('脚本自身异常：%s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 2

    now = datetime.datetime.now()
    ok_n = sum(1 for r in results if r['ok'])
    total = len(results)
    payload = {
        'date': now.strftime('%Y-%m-%d %H:%M'),
        'summary': '%d/%d' % (ok_n, total),
        'results': results,
    }

    # 签到 NAS 端核对（不进分母，只在日志体现）
    ci_ok, ci_detail = checkin_nas_today()
    print('签到 NAS 端：%s（%s）' % ('当天已执行' if ci_ok else '未执行/未知', ci_detail))

    try:
        push_github(payload)
    except Exception as e:
        print('推送 GitHub 失败：%s' % e, file=sys.stderr)
        return 2

    print('快照已更新：%s  %s  %s' % (payload['date'], payload['summary'],
          '正常' if ok_n == total else '⚠️ %d 项异常' % (total - ok_n)))
    for r in results:
        if not r['ok']:
            print('  异常 %s：%s' % (r['item'], r['detail']))
    print('范围说明：签到 NAS 端与验证码工具不占分母；云端 automation 已停。')
    return 0 if ok_n == total else 1


if __name__ == '__main__':
    sys.exit(main())

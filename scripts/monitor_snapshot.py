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
  4. 写操作：把快照 PUT 到 GitHub 仓库 tools-hub 的 data/latest.json，
     并追加一条记录到 data/history.json 供入口站日志页查询历史。
     历史写入失败只告警，不影响主快照；PAT 从环境变量 GH_PAT_TOOLSHUB 读取，不打印。

环境变量（青龙「环境变量」里配置，脚本本身不含任何密钥）：
  GH_PAT_TOOLSHUB  GitHub PAT（需对 elizond0/tools-hub 有 push 权限）

分母口径：固定 10 项 = 地址文件 2 + 端点 8；签到 NAS 端当天执行则另记 1 项（说明列），
不进 results 数组（保持入口站兼容），只在 stdout 日志体现。验证码工具永不纳入。

退出码：0 正常 / 1 探测到异常 / 2 脚本自身异常（含缺密钥、写 GitHub 失败）
"""
import sys, json, os, base64, datetime, urllib.request, urllib.error, ssl

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
HISTORY_PATH = 'data/history.json'
HISTORY_LIMIT = 240   # 每 8 小时 3 次 ≈ 80 天；超量丢最旧，控制文件体积


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


def _api_url(path):
    return 'https://api.github.com/repos/%s/contents/%s' % (REPO, path)


def _auth_headers(pat):
    return {'Authorization': 'Bearer ' + pat,
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'tools-hub-monitor/1.0'}


def _get_json_file(path, pat):
    """读取仓库 JSON 文件，返回 (对象, sha)；404 返回 (None, '')。"""
    st, b, _ = http(_api_url(path), headers=_auth_headers(pat), timeout=TIMEOUT)
    if st == 404:
        return None, ''
    if st != 200:
        raise RuntimeError('读取 %s 失败 HTTP %d' % (path, st))
    j = json.loads(b.decode('utf-8'))
    content = base64.b64decode(j.get('content', '')).decode('utf-8')
    return json.loads(content), j.get('sha', '')


def _put_json_file(path, obj, message, sha, pat):
    """新建或更新仓库 JSON 文件。"""
    content = base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')).decode()
    body_obj = {'message': message, 'content': content}
    if sha:
        body_obj['sha'] = sha
    body = json.dumps(body_obj).encode('utf-8')
    st, b, _ = http(_api_url(path), method='PUT', headers=_auth_headers(pat),
                    body=body, timeout=TIMEOUT)
    if st not in (200, 201):
        raise RuntimeError('写入 %s 失败 HTTP %d: %s' % (
            path, st, b.decode('utf-8', 'ignore')[:120]))


def push_github(payload):
    """把最新快照 PUT 到 GitHub（覆盖式）。PAT 从环境变量读，不打印。"""
    pat = os.environ.get('GH_PAT_TOOLSHUB', '').strip()
    if not pat:
        raise RuntimeError('缺环境变量 GH_PAT_TOOLSHUB')
    _, sha = _get_json_file(PATH, pat)
    _put_json_file(PATH, payload,
                   'monitor: auto snapshot %s' % payload['date'], sha, pat)
    return True


def push_history(payload):
    """把本次结果追加到历史文件。失败只告警，不影响主快照。"""
    pat = os.environ.get('GH_PAT_TOOLSHUB', '').strip()
    if not pat:
        return False, '缺环境变量 GH_PAT_TOOLSHUB'
    try:
        old, sha = _get_json_file(HISTORY_PATH, pat)
        if isinstance(old, dict):
            records = old.get('records') if isinstance(old.get('records'), list) else []
        elif isinstance(old, list):
            records = old
        else:
            records = []
        rec = {'date': payload['date'], 'summary': payload['summary'],
               'results': payload['results']}
        # 同一分钟重跑只保留最后一次，避免重复
        records = [r for r in records if not (isinstance(r, dict) and r.get('date') == rec['date'])]
        records.append(rec)
        records = records[-HISTORY_LIMIT:]
        obj = {'version': 1,
               'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'count': len(records),
               'records': records}
        _put_json_file(HISTORY_PATH, obj,
                       'monitor: append history %s' % payload['date'], sha, pat)
        return True, str(len(records))
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, e)


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

    hist_ok, hist_note = push_history(payload)
    if not hist_ok:
        print('历史记录写入失败（不影响本次快照）：%s' % hist_note, file=sys.stderr)

    print('快照已更新：%s  %s  %s' % (payload['date'], payload['summary'],
          '正常' if ok_n == total else '⚠️ %d 项异常' % (total - ok_n)))
    for r in results:
        if not r['ok']:
            print('  异常 %s：%s' % (r['item'], r['detail']))
    print('范围说明：签到 NAS 端与验证码工具不占分母；云端 automation 已停。')
    return 0 if ok_n == total else 1


if __name__ == '__main__':
    sys.exit(main())

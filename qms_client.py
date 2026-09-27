# -*- coding: utf-8 -*-
"""QMediaSync 对接模块

QMS 提供的 HTTP API（本模块据此实现）：
- 连接测试        GET  /api/user/info
- 列同步路径      GET  /api/sync/path-list        → 生成 strm 的目标
- 触发生成 strm   POST /api/sync/path/start       body: {"id": <同步路径 ID>}
  （返回项里的 is_running：0 未运行 / 1 已在队列 / 2 正在运行，可轮询等待完成）
- 列刮削任务      GET  /api/scrape/pathes
- 触发刮削启动    POST /api/scrape/pathes/start   body: {"id": <刮削 ID>}

鉴权：优先 API Key（请求头 X-API-Key），其次账号密码（POST /api/login + X-CSRF-Token）

## 一条连接的完整流程

    转存到新文件 →（可选）触发同步路径生成 strm → 等待 → 触发刮削

QMS 里「生成 strm」和「刮削」是两个独立动作：前者把 115 上的视频映射成本地
.strm 文件，后者才去 TMDB 抓元数据。**顺序不能反** —— 没有 strm 就刮不到东西。
所以每条连接可以额外绑定一个「同步路径」，并选择等待策略：

    poll  ：轮询同步路径的 is_running 直到回到 0（最稳，等真正干完）
    delay ：固定等 N 秒（QMS 简单场景够用）
    none  ：不等待，触发完 strm 立即刮削（旧行为）

不绑定同步路径时行为与以前一致 —— 直接刮削，完全向后兼容。

由于 115 的目录在 QMS 眼里是挂载到本地的路径（CloudDrive2 / 其它挂载），
所以绑定关系需要人工指定 —— 即本模块的 links。
"""
import threading
import time
import uuid

import requests

from history_db import get_kv, set_kv, record_qms_log

DEFAULT_PORT = 12333
TIMEOUT = 15

# 「先生成 strm → 再刮削」的等待策略
WAIT_MODES = [
    ('poll', '轮询等待 strm 同步结束（推荐，最稳）'),
    ('delay', '固定延迟若干秒'),
    ('none', '不等待，触发后立即刮削'),
]
DEFAULT_WAIT_MODE = 'poll'
DEFAULT_WAIT_SECONDS = 15
DEFAULT_WAIT_TIMEOUT = 900

# QMS 配置与连接列表存 SQLite（config.json 会被转存进度频繁回写，放里面会被覆盖）
QMS_KEY = 'qms'

_sessions = {}
_lock = threading.Lock()


def load_cfg():
    """读取 QMS 配置（含连接列表）"""
    cfg = get_kv(QMS_KEY)
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_cfg(cfg):
    """保存 QMS 配置（含连接列表）"""
    set_kv(QMS_KEY, dict(cfg or {}))


def normalize_config(cfg):
    """整理连接配置（地址栏允许直接粘 http://ip:port）"""
    cfg = dict(cfg or {})
    scheme = (cfg.get('scheme') or 'http').strip() or 'http'
    host = (cfg.get('host') or '').strip()
    port = cfg.get('port')

    if '://' in host:
        scheme, _, host = host.partition('://')
    host = host.strip().strip('/')
    if '/' in host:
        host = host.split('/', 1)[0]
    if ':' in host:
        host, _, tail = host.partition(':')
        if not port:
            port = tail
    try:
        port = int(port) if port else DEFAULT_PORT
    except (TypeError, ValueError):
        port = DEFAULT_PORT

    return {
        'enabled': bool(cfg.get('enabled')),
        'scheme': scheme,
        'host': host,
        'port': port,
        'auth_mode': (cfg.get('auth_mode') or 'api_key').strip() or 'api_key',
        'api_key': (cfg.get('api_key') or '').strip(),
        'username': (cfg.get('username') or '').strip(),
        'password': cfg.get('password') or '',
        'auto_trigger': cfg.get('auto_trigger', True) is not False,
        'delay_seconds': int(cfg.get('delay_seconds') or 0),
    }


def get_links(cfg):
    """取出连接列表（只返回结构正常的条目）"""
    links = (cfg or {}).get('links') or []
    out = []
    for l in links:
        if not isinstance(l, dict):
            continue
        out.append({
            'id': str(l.get('id') or ''),
            'task_uid': str(l.get('task_uid') or ''),
            'task_order': l.get('task_order'),
            'task_name': str(l.get('task_name') or ''),
            'qms_id': l.get('qms_id'),
            'qms_path': str(l.get('qms_path') or ''),
            'qms_media_type': str(l.get('qms_media_type') or ''),
            'scope': str(l.get('scope') or 'task'),   # task=绑定某个转存任务；offline=离线下载完成后触发
            'enabled': l.get('enabled', True) is not False,
            'created_at': str(l.get('created_at') or ''),
            # ---- 先生成 strm，再刮削 ----
            'sync_path_id': l.get('sync_path_id') or None,
            'sync_path_name': str(l.get('sync_path_name') or ''),
            'wait_mode': str(l.get('wait_mode') or DEFAULT_WAIT_MODE),
            'wait_seconds': int(l.get('wait_seconds') or DEFAULT_WAIT_SECONDS),
            'wait_timeout': int(l.get('wait_timeout') or DEFAULT_WAIT_TIMEOUT),
        })
    return out


def new_link_id():
    return uuid.uuid4().hex[:8]


def match_links_for_task(links, task):
    """找出绑定到某个任务的连接（task_uid 优先，其次 order，再退化为名称）"""
    if not task:
        return []
    uid = str(task.get('task_uid') or '')
    order = task.get('order')
    name = str(task.get('name') or '')
    hit = []
    for l in links:
        if not l.get('enabled', True):
            continue
        if str(l.get('scope') or 'task') != 'task':
            continue          # scope=offline 的连接不参与转存任务绑定
        if uid and l.get('task_uid') and l['task_uid'] == uid:
            hit.append(l)
            continue
        if order is not None and l.get('task_order') is not None:
            try:
                if int(l['task_order']) == int(order):
                    hit.append(l)
                    continue
            except (TypeError, ValueError):
                pass
        if name and l.get('task_name') and l['task_name'] == name:
            hit.append(l)
    return hit


def merge_cfg(saved, incoming):
    """把「已保存的配置」和「请求里临时传来的连接参数」合并。

    用途：点「测试连接」「刷新刮削目录」时应当用页面上当前填的值，不必先保存。
    - 普通字段：请求里带了就用请求的；
    - 密钥字段（api_key/password）：请求里非空才覆盖，留空表示沿用已保存的。
    """
    cfg = dict(saved or {})
    incoming = dict(incoming or {})
    for key in ('enabled', 'host', 'port', 'scheme', 'auth_mode', 'username', 'delay_seconds'):
        if key in incoming:
            cfg[key] = incoming[key]
    for key in ('api_key', 'password'):
        if str(incoming.get(key) or '').strip():
            cfg[key] = incoming[key]
    if incoming.get('clear_api_key'):
        cfg['api_key'] = ''
    if incoming.get('clear_password'):
        cfg['password'] = ''
    return cfg


class QmsError(Exception):
    """QMediaSync 调用异常（消息可直接展示给用户）"""


class QmsClient:
    def __init__(self, cfg):
        self.cfg = normalize_config(cfg)
        if not self.cfg['host']:
            raise QmsError('未填写 QMediaSync 地址')
        self.base = '%s://%s:%s/api' % (self.cfg['scheme'], self.cfg['host'], self.cfg['port'])

    # ---------------- 鉴权 ----------------

    def _login(self):
        key = self.base + '|' + self.cfg['username']
        with _lock:
            cached = _sessions.get(key)
            if cached and cached.get('exp', 0) > time.time():
                return cached
        if not self.cfg['username'] or not self.cfg['password']:
            raise QmsError('未填写 QMediaSync 用户名或密码')
        sess = requests.Session()
        try:
            resp = sess.post(
                self.base + '/login',
                json={'username': self.cfg['username'],
                      'password': self.cfg['password'],
                      'rememberMe': True},
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise QmsError(self._conn_error(e))
        data = self._json(resp)
        if data.get('code') != 200:
            raise QmsError(data.get('message') or '登录失败（检查用户名/密码）')
        payload = data.get('data') or {}
        csrf = payload.get('csrf_token') or sess.cookies.get('csrf_token') or ''
        cached = {'sess': sess, 'csrf': csrf, 'exp': time.time() + 1800}
        with _lock:
            _sessions[key] = cached
        return cached

    def _prepare(self):
        headers = {'Content-Type': 'application/json'}
        if self.cfg['auth_mode'] == 'password':
            info = self._login()
            if info.get('csrf'):
                headers['X-CSRF-Token'] = info['csrf']
            return info['sess'], headers
        if not self.cfg['api_key']:
            raise QmsError('未填写 API Key（在 QMediaSync 的「API 密钥」页面创建）')
        headers['X-API-Key'] = self.cfg['api_key']
        return requests.Session(), headers

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except ValueError:
            raise QmsError('返回内容不是 JSON（HTTP %s，可能地址/端口填错）' % resp.status_code)

    def _conn_error(self, e):
        """把底层网络异常翻译成人话"""
        host = '%s:%s' % (self.cfg['host'], self.cfg['port'])
        text = str(e)
        if 'Connection refused' in text or 'NewConnectionError' in text or 'Max retry' in text:
            return '连不上 %s（连接被拒绝），检查地址、端口，以及 QMediaSync 是否在运行' % host
        if 'timed out' in text.lower():
            return '连接 %s 超时，检查网络或防火墙' % host
        if 'Name or service not known' in text or 'nodename nor servname' in text:
            return '地址 %s 解析不了，检查填的地址对不对' % host
        return '连接 %s 失败：%s' % (host, text)

    def _request(self, method, path, payload=None, params=None, _retry=True):
        sess, headers = self._prepare()
        try:
            resp = getattr(sess, method)(
                self.base + path, headers=headers, json=payload, params=params, timeout=TIMEOUT
            )
        except requests.exceptions.RequestException as e:
            raise QmsError(self._conn_error(e))
        if resp.status_code == 401 and _retry and self.cfg['auth_mode'] == 'password':
            with _lock:
                _sessions.pop(self.base + '|' + self.cfg['username'], None)
            return self._request(method, path, payload, params, _retry=False)
        if resp.status_code in (401, 403):
            raise QmsError('鉴权失败（API Key 无效或未授权）')
        data = self._json(resp)
        if not isinstance(data, dict):
            raise QmsError('返回格式异常')
        return data

    # ---------------- 对外能力 ----------------

    def test(self):
        data = self._request('get', '/user/info')
        if data.get('code') != 200:
            raise QmsError(data.get('message') or '连接失败')
        return {'username': (data.get('data') or {}).get('username') or '', 'base': self.base}

    def list_scrape_paths(self):
        data = self._request('get', '/scrape/pathes')
        if data.get('code') != 200:
            raise QmsError(data.get('message') or '获取刮削任务失败')
        out = []
        for it in (data.get('data') or []):
            out.append({
                'id': it.get('id'),
                'media_type': it.get('media_type') or '',
                'source_path': it.get('source_path') or '',
                'source_type': it.get('source_type') or '',
                'scrape_type': it.get('scrape_type') or '',
                'enable_cron': bool(it.get('enable_cron')),
                'cron_expression': it.get('cron_expression') or '',
            })
        out.sort(key=lambda x: (x['id'] is None, x['id']))
        return out

    # ---------------- 同步路径（生成 strm）----------------

    def list_sync_paths(self):
        """列出 QMS 的同步路径（用于「先生成 strm」下拉）

        注意：QMS 的 SyncPath **没有 name 字段**，所以展示名用远程路径兜底。
        is_running: 0 未运行 / 1 已在队列 / 2 正在运行
        """
        data = self._request('get', '/sync/path-list',
                             params={'page': 1, 'page_size': 200})
        if data.get('code') != 200:
            raise QmsError(data.get('message') or '获取同步路径失败')
        payload = data.get('data')
        if isinstance(payload, dict):
            items = payload.get('list') or []
        else:
            items = payload or []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sid = it.get('id')
            out.append({
                'id': sid,
                'name': (it.get('name') or it.get('remote_path')
                         or ('同步路径 #%s' % sid)),
                'remote_path': it.get('remote_path') or '',
                'local_path': it.get('local_path') or '',
                'source_type': str(it.get('source_type') or ''),
                'account_name': it.get('account_name') or '',
                'enable_cron': bool(it.get('enable_cron')),
                'is_full_sync': bool(it.get('is_full_sync')),
                'is_running': int(it.get('is_running') or 0),
                'last_sync_at': it.get('last_sync_at') or 0,
            })
        out.sort(key=lambda x: (x['id'] is None, x['id']))
        return out

    def get_sync_running(self, sync_path_id):
        """查某个同步路径的运行状态：0 未运行 / 1 已在队列 / 2 正在运行"""
        for it in self.list_sync_paths():
            if str(it.get('id')) == str(sync_path_id):
                return int(it.get('is_running') or 0)
        return 0

    def start_sync(self, sync_path_id):
        """触发指定同步路径生成 strm"""
        data = self._request('post', '/sync/path/start', {'id': int(sync_path_id)})
        ok = data.get('code') == 200
        return {'ok': ok,
                'message': data.get('message') or ('已加入同步队列' if ok else '触发失败'),
                'data': data.get('data') or {}}

    def wait_sync_done(self, sync_path_id, timeout=DEFAULT_WAIT_TIMEOUT, interval=5):
        """轮询等待同步任务结束（is_running 回到 0）

        返回 (是否正常结束, 说明)。超时**不算异常** —— 调用方通常仍会继续刮削，
        因为已经生成的那部分 strm 依然值得刮。

        为什么要先等一下再轮询：刚触发时任务可能还没进队列，立刻查会看到 0，
        被误判成"已经干完了"。
        """
        started = time.time()
        seen_running = False
        time.sleep(min(interval, 5))          # 给任务一点时间入队
        while True:
            elapsed = time.time() - started
            if elapsed > timeout:
                return False, ('等待同步超时（%d 秒）。可调大「最长等待」，'
                               '或把等待方式改成「固定延迟」。' % timeout)
            try:
                st = self.get_sync_running(sync_path_id)
            except Exception as e:  # noqa: BLE001
                return False, '查询同步状态失败：%s' % str(e)[:120]
            if st in (1, 2):
                seen_running = True
            elif st == 0 and seen_running:
                return True, 'strm 同步已完成（用时 %.0f 秒）' % elapsed
            elif st == 0 and not seen_running and elapsed > 20:
                # 始终没出现运行态：多半是任务已经秒完（没有新文件时也会很快结束）
                return True, '同步任务已结束（未捕获到运行态，可能没有新文件需要生成）'
            time.sleep(interval)

    # ---------------- 刮削 ----------------

    def start(self, ids=None):
        ids = [int(i) for i in (ids or [])]
        if not ids:
            raise QmsError('还没有绑定任何刮削任务')
        results = []
        for tid in ids:
            try:
                data = self._request('post', '/scrape/pathes/start', {'id': int(tid)})
                ok = data.get('code') == 200
                results.append({'id': tid, 'ok': ok,
                                'message': data.get('message') or ('已开始' if ok else '触发失败')})
            except Exception as e:  # noqa: BLE001
                results.append({'id': tid, 'ok': False, 'message': str(e)})
        return results


def build_summary(results):
    ok_ids = [r['id'] for r in results if r.get('ok')]
    fails = [r for r in results if not r.get('ok')]
    parts = []
    if ok_ids:
        parts.append('已触发刮削 ' + '、'.join('#%s' % i for i in ok_ids))
    for r in fails:
        parts.append('#' + str(r.get('id')) + ' 失败：' + str(r.get('message') or ''))
    return '；'.join(parts) or '没有可触发的刮削任务'


def build_pipeline_summary(steps):
    """把步骤列表压成一句话（给日志 / 通知 / 界面用）"""
    parts = []
    for s in steps:
        detail = s.get('detail') or ''
        if s.get('step') == '继续':
            parts.append(detail)              # 「超时仍继续」这类提示不重复标步骤名
            continue
        mark = '' if s.get('ok') else '（失败）'
        parts.append('%s%s%s' % (s.get('step'), mark, ('：' + detail) if detail else ''))
    return ' → '.join(parts) or '没有执行任何步骤'


def run_link_pipeline(link, source='auto', dry_run=False):
    """执行一条连接的完整流程：生成 strm → 等待 → 刮削

    返回 (ok, summary, steps)
      steps: [{'step','ok','detail'}, ...]，供触发日志与界面展示

    兼容旧配置：连接里没有 sync_path_id 时，等价于「直接刮削」。
    """
    sync_id = link.get('sync_path_id') or None
    scrape_id = link.get('qms_id')
    scrape_ok = str(scrape_id or '').isdigit()
    mode = str(link.get('wait_mode') or DEFAULT_WAIT_MODE)

    if dry_run:
        parts = []
        if sync_id:
            parts.append('生成 strm（同步路径 #%s）' % sync_id)
            if mode == 'poll':
                parts.append('轮询等待同步结束（最长 %s 秒）'
                             % (link.get('wait_timeout') or DEFAULT_WAIT_TIMEOUT))
            elif mode == 'delay':
                parts.append('固定等待 %s 秒'
                             % (link.get('wait_seconds') or DEFAULT_WAIT_SECONDS))
        if scrape_ok:
            parts.append('刮削（刮削任务 #%s）' % scrape_id)
        return True, '（预演）' + ' → '.join(parts or ['没有可用步骤']), []

    steps = []
    client = QmsClient(load_cfg())

    # ---- 第 1 步：生成 strm ----
    if sync_id:
        try:
            r = client.start_sync(sync_id)
        except Exception as e:  # noqa: BLE001
            r = {'ok': False, 'message': str(e)}
        steps.append({'step': '生成 strm', 'ok': bool(r.get('ok')),
                      'detail': r.get('message') or ''})
        if not r.get('ok'):
            return False, build_pipeline_summary(steps), steps

        # ---- 第 2 步：等待 strm 落地 ----
        if mode == 'poll':
            ok, msg = client.wait_sync_done(
                sync_id, timeout=int(link.get('wait_timeout') or DEFAULT_WAIT_TIMEOUT))
            steps.append({'step': '等待 strm 完成', 'ok': ok, 'detail': msg})
            if not ok:
                steps.append({'step': '继续', 'ok': True,
                              'detail': '等待超时，仍继续刮削（已生成的 strm 会被刮到）'})
        elif mode == 'delay':
            sec = max(int(link.get('wait_seconds') or DEFAULT_WAIT_SECONDS), 0)
            time.sleep(sec)
            steps.append({'step': '等待', 'ok': True, 'detail': '固定延迟 %d 秒' % sec})

    # ---- 第 3 步：刮削 ----
    if scrape_ok:
        try:
            res = client.start([int(scrape_id)])
            r = res[0] if res else {'ok': False, 'message': '没有返回结果'}
        except Exception as e:  # noqa: BLE001
            r = {'ok': False, 'message': str(e)}
        steps.append({'step': '刮削', 'ok': bool(r.get('ok')),
                      'detail': r.get('message') or ''})
    elif not sync_id:
        steps.append({'step': '跳过', 'ok': False,
                      'detail': '这条连接既没绑定同步路径，也没绑定刮削任务'})

    ok = all(s['ok'] for s in steps) if steps else False
    return ok, build_pipeline_summary(steps), steps


def trigger_link(link, source='manual', keep=30):
    """触发一条连接的完整流程（生成 strm → 等待 → 刮削），并写入触发日志

    link: 连接字典（id / sync_path_id / qms_id / wait_mode / task_name）
    返回 {id, ok, message, steps}
    """
    try:
        ok, summary, steps = run_link_pipeline(link, source=source)
    except Exception as e:  # noqa: BLE001
        ok, summary, steps = False, '触发失败：%s' % e, []
    try:
        record_qms_log(link_id=link.get('id'), task_name=link.get('task_name') or '',
                       qms_id=link.get('qms_id'), success=bool(ok),
                       message=summary, source=source, keep=keep)
    except Exception:  # noqa: BLE001
        pass
    return {'id': link.get('qms_id'), 'ok': bool(ok), 'message': summary, 'steps': steps}


def trigger_after_transfer(task, transferred_count, dry_run=False, source='auto'):
    """任务转存到新文件后，触发它绑定的 QMS 刮削任务。

    task 传任务字典（用 task_uid/order/name 匹配连接列表）；
    dry_run 只做匹配不真触发（用于自检）。
    """
    cfg = load_cfg()
    norm = normalize_config(cfg)
    if not norm['enabled'] or not norm['auto_trigger']:
        return None
    if not transferred_count:
        return None

    links = match_links_for_task(get_links(cfg), task)
    if not links:
        return None

    valid = [l for l in links
             if l.get('sync_path_id') or str(l.get('qms_id') or '').isdigit()]
    if not valid:
        return None

    task_name = (task or {}).get('name') or ('任务%s' % (task or {}).get('order', ''))
    if dry_run:
        parts = [run_link_pipeline(l, dry_run=True)[1] for l in valid]
        return '（预演）任务「%s」：%s' % (task_name, '；'.join(parts))

    delay = norm.get('delay_seconds') or 0
    if delay > 0:
        time.sleep(delay)

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        results = [trigger_link(l, source=source) for l in valid]
        summary = build_summary(results)
        cfg['last_trigger_at'] = now
        cfg['last_trigger_result'] = summary
        cfg['last_trigger_task'] = task_name
        cfg['last_trigger_ok'] = all(r.get('ok') for r in results)
        save_cfg(cfg)
        return summary
    except Exception as e:  # noqa: BLE001
        cfg['last_trigger_at'] = now
        cfg['last_trigger_result'] = '触发失败：%s' % e
        cfg['last_trigger_task'] = task_name
        cfg['last_trigger_ok'] = False
        save_cfg(cfg)
        return '触发失败：%s' % e


def match_links_for_offline(links):
    """离线下载完成后要触发的连接：scope == 'offline' 且启用中"""
    return [l for l in (links or []) if l.get('enabled', True) and str(l.get('scope') or 'task') == 'offline']


def trigger_offline_done(task_name='离线下载', dry_run=False, source='offline'):
    """离线下载任务完成后，触发 scope=offline 的连接"""
    cfg = load_cfg()
    norm = normalize_config(cfg)
    if not norm['enabled'] or not norm['auto_trigger']:
        return None
    links = [l for l in match_links_for_offline(get_links(cfg))
             if l.get('sync_path_id') or str(l.get('qms_id') or '').isdigit()]
    if not links:
        return None
    if dry_run:
        parts = [run_link_pipeline(l, dry_run=True)[1] for l in links]
        return '（预演）离线下载完成后：%s' % '；'.join(parts)
    results = []
    for l in links:
        link = dict(l)
        link['task_name'] = task_name
        results.append(trigger_link(link, source=source))
    summary = build_summary(results)
    cfg['last_trigger_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    cfg['last_trigger_result'] = summary
    cfg['last_trigger_task'] = task_name
    cfg['last_trigger_ok'] = all(r.get('ok') for r in results)
    save_cfg(cfg)
    return summary

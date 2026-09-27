# -*- coding: utf-8 -*-
"""配置备份与恢复

备份范围 = config.json（账号 / 转存任务 / 各类设置） + SQLite 里的 QMS 联动配置与历史。

三个设计原则：
1. **默认带上账号 Cookie** —— 否则恢复完还得一个个重新登录。界面上有开关，
   关掉之后导出的文件可以放心发给别人帮忙排查问题。
2. **恢复前一定先自动存一份当前状态**（tag=before-restore）。恢复是覆盖写，
   万一拿错文件，还能原样退回来。
3. **备份文件必须自证身份** —— 靠 app / kind / format 三个字段校验，
   避免误把别的 JSON 导进来把配置冲掉。

另外提供「服务器本地备份」：文件落在 config/backups/ 下。NAS 上倒腾文件很烦，
能在面板里直接挑一份恢复会省事很多。
"""
import json
import os
import re
import time

from history_db import dump_tables, restore_tables

APP_ID = '115SavePro'
KIND = 'backup'
FORMAT = 1

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, 'config', 'backups')
MAX_LOCAL = 20               # 本地最多保留多少份
MAX_BYTES = 32 * 1024 * 1024  # 单个备份文件上限 32MB
LIST_PARSE_BYTES = 4 * 1024 * 1024   # 列表页解析摘要的上限（超过就只显示大小，避免卡页面）

# 可恢复的部件（顺序即界面展示顺序）
PARTS = [
    ('users',    '账号（含 Cookie）'),
    ('tasks',    '转存任务'),
    ('qms',      'QMS 联动配置'),
    ('notify',   '通知渠道'),
    ('settings', '系统设置（定时 / 空间告警 / 正则等）'),
    ('auth',     '后台登录账号密码'),
    ('history',  '历史记录（转存 / 触发 / 离线）'),
]
PART_KEYS = [k for k, _ in PARTS]
PART_LABELS = dict(PARTS)

# 属于「系统设置」的 config 段落
SETTING_SECTIONS = ('cron', 'scheduler', 'quota_alert', 'regex',
                    'file_operations', 'offline', 'update', 'task')

_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+\.json$')
_QMS_KEY = 'qms'


class BackupError(Exception):
    """备份/恢复过程中的可读错误"""


def _human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------

def build_payload(config, include_cookies=True, include_auth=True, include_history=True):
    """把当前状态打包成备份结构

    config: storage.config（活的配置字典）
    返回可直接 json.dump 的 dict
    """
    cfg = json.loads(json.dumps(config or {}, ensure_ascii=False))  # 深拷贝，不动原对象
    p115 = cfg.get('p115') or {}

    # --- 账号 Cookie ---
    if not include_cookies:
        for name, u in (p115.get('users') or {}).items():
            if isinstance(u, dict) and u.get('cookies'):
                u['cookies'] = ''
                u['cookies_omitted'] = True

    # --- 后台密码 ---
    if not include_auth:
        cfg['auth'] = {}

    payload = {
        'app': APP_ID,
        'kind': KIND,
        'format': FORMAT,
        'version': _app_version(),
        'exported_at': _now(),
        'options': {
            'cookies': bool(include_cookies),
            'auth': bool(include_auth),
            'history': bool(include_history),
        },
        'config': cfg,
        'qms': _safe_kv(_QMS_KEY),
    }
    if include_history:
        payload['history'] = dump_tables()
    payload['summary'] = summarize(payload)
    return payload


def _app_version():
    try:
        import web_app
        return getattr(web_app, 'APP_VERSION', '')
    except Exception:  # noqa: BLE001
        return ''


def _safe_kv(key):
    try:
        from history_db import get_kv
        v = get_kv(key)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def summarize(payload):
    """统计备份里有什么（用于界面展示，不加载全部内容）"""
    payload = payload or {}
    cfg = payload.get('config') or {}
    p115 = cfg.get('p115') or {}
    users = p115.get('users') or {}
    tasks = p115.get('tasks') or []
    qms = payload.get('qms') or {}
    hist = payload.get('history') or {}
    n_hist = sum(len(v) for v in hist.values() if isinstance(v, list))

    with_cookie = sum(1 for u in users.values()
                      if isinstance(u, dict) and u.get('cookies'))
    return {
        'users': len(users),
        'users_with_cookie': with_cookie,
        'tasks': len(tasks),
        'qms_links': len(qms.get('links') or []),
        'qms_enabled': bool(qms.get('enabled')),
        'history': n_hist,
        'has_auth': bool((cfg.get('auth') or {}).get('username')),
        'has_history': bool(hist),
        'notify_on': bool((cfg.get('notify') or {}).get('enabled')),
        'current_user': p115.get('current_user') or '',
    }


# --------------------------------------------------------------------------
# 本地备份文件
# --------------------------------------------------------------------------

def _local_path(name):
    """把备份文件名解析成绝对路径，并挡住目录穿越"""
    name = str(name or '')
    if not _NAME_RE.match(name):
        raise BackupError('备份文件名不合法')
    path = os.path.abspath(os.path.join(BACKUP_DIR, name))
    if not path.startswith(os.path.abspath(BACKUP_DIR) + os.sep):
        raise BackupError('备份文件名不合法')
    return path


def local_name(tag='manual'):
    tag = re.sub(r'[^A-Za-z0-9_-]', '', str(tag or 'manual'))[:20] or 'manual'
    return 'backup-%s-%s.json' % (time.strftime('%Y%m%d-%H%M%S'), tag)


def save_local(payload, tag='manual'):
    """把备份写到服务器本地目录，返回文件名"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = local_name(tag)
    path = os.path.join(BACKUP_DIR, name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    prune_local()
    return name


def list_local():
    """列出本地备份（新的在前）"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = []
    for fn in os.listdir(BACKUP_DIR):
        if not fn.endswith('.json') or not _NAME_RE.match(fn):
            continue
        path = os.path.join(BACKUP_DIR, fn)
        try:
            size = os.path.getsize(path)
            mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(path)))
        except OSError:
            continue
        tag = 'manual'
        m = re.match(r'^backup-\d{8}-\d{6}-(.+)\.json$', fn)
        if m:
            tag = m.group(1)
        item = {'name': fn, 'size': size, 'size_text': _human_size(size),
                'created_at': mtime, 'tag': tag,
                'tag_text': {'manual': '手动导出', 'before-restore': '恢复前自动存档',
                             'auto': '自动'}.get(tag, tag),
                'summary': None, 'invalid': False}
        # 摘要：读文件解析；太大或坏掉了就标记出来，但仍列出（方便删除/下载）
        if size > LIST_PARSE_BYTES:
            item['summary'] = None
        else:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    item['summary'] = summarize(json.load(f))
            except Exception:  # noqa: BLE001
                item['invalid'] = True
                item['error'] = '文件内容无法解析'
        out.append(item)
    out.sort(key=lambda x: x['name'], reverse=True)
    return out


def load_local(name):
    """读取一个本地备份"""
    path = _local_path(name)
    if not os.path.exists(path):
        raise BackupError('备份不存在：%s' % name)
    if os.path.getsize(path) > MAX_BYTES:
        raise BackupError('备份文件过大（超过 %s）' % _human_size(MAX_BYTES))
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise BackupError('读取备份失败：%s' % e)


def delete_local(name):
    path = _local_path(name)
    if not os.path.exists(path):
        raise BackupError('备份不存在：%s' % name)
    try:
        os.remove(path)
    except OSError as e:
        raise BackupError('删除失败：%s' % e)
    return True


def prune_local(keep=MAX_LOCAL):
    """只保留最近的 keep 份，优先清理自动存档"""
    files = []
    try:
        for fn in os.listdir(BACKUP_DIR):
            if fn.endswith('.json') and _NAME_RE.match(fn):
                p = os.path.join(BACKUP_DIR, fn)
                files.append((os.path.getmtime(p), fn))
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    files.sort()                       # 旧的在前
    removed = 0
    for _, fn in files[:len(files) - keep]:
        try:
            os.remove(os.path.join(BACKUP_DIR, fn))
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# 校验与预览
# --------------------------------------------------------------------------

def validate(payload):
    """检查是不是我们自己的备份文件，返回规范化后的 payload"""
    if isinstance(payload, str):
        payload = parse_text(payload)
    if not isinstance(payload, dict):
        raise BackupError('备份内容不是一个 JSON 对象')
    if payload.get('app') != APP_ID:
        raise BackupError('这不是 115SavePro 的备份文件（app 字段为 %r）' % payload.get('app'))
    if payload.get('kind') != KIND:
        raise BackupError('文件类型不对（kind=%r），请选择导出的备份文件' % payload.get('kind'))
    fmt = payload.get('format')
    if not isinstance(fmt, int) or fmt < 1:
        raise BackupError('备份格式无法识别（format=%r）' % fmt)
    if fmt > FORMAT:
        raise BackupError('备份来自更新的版本（format=%s，本机只认 %s），请先升级程序' % (fmt, FORMAT))
    if not isinstance(payload.get('config'), dict):
        raise BackupError('备份里没有配置数据')
    return payload


def parse_text(text):
    """把上传/粘贴的文本解析成 JSON，并给出人话错误"""
    text = (text or '').strip()
    if not text:
        raise BackupError('备份内容为空')
    if len(text.encode('utf-8', 'ignore')) > MAX_BYTES:
        raise BackupError('备份内容过大（超过 %s）' % _human_size(MAX_BYTES))
    try:
        return json.loads(text)
    except ValueError as e:
        raise BackupError('不是合法的 JSON：%s' % str(e)[:120])


def preview(payload):
    """给界面用的预览信息：摘要 + 备份里实际包含哪些部件"""
    payload = validate(payload)
    opts = payload.get('options') or {}
    s = payload.get('summary') or summarize(payload)
    available = ['users', 'tasks', 'qms', 'notify', 'settings']
    if (payload.get('config') or {}).get('auth'):
        available.append('auth')
    if payload.get('history'):
        available.append('history')
    # 账号里一个 Cookie 都没有（导出时被剔除）→ 恢复账号没意义，直接从选项里拿掉
    if s.get('users') and not s.get('users_with_cookie'):
        available = [p for p in available if p != 'users']
    return {
        'app': payload.get('app'),
        'version': payload.get('version') or '',
        'exported_at': payload.get('exported_at') or '',
        'options': opts,
        'summary': s,
        'available_parts': [{'key': k, 'label': PART_LABELS.get(k, k)} for k in PART_KEYS if k in available],
        'cookies_omitted': not bool(opts.get('cookies')),
    }


# --------------------------------------------------------------------------
# 应用（恢复）
# --------------------------------------------------------------------------

def apply_payload(current_config, payload, mode='replace', parts=None, do_history=True):
    """把备份应用到当前配置上

    current_config: 当前 storage.config（会被原地修改）
    mode: replace=整体覆盖 / merge=按主键合并（账号、任务、QMS 连接）
    parts: 要恢复的部件列表，None=全部
    返回 (new_config, report)
    """
    payload = validate(payload)
    cfg_in = json.loads(json.dumps(payload.get('config') or {}, ensure_ascii=False))
    qms_in = payload.get('qms') or {}
    hist_in = payload.get('history') or {}

    # parts=None 表示全部；**显式传空列表要报错**，不能因为 [] 是假值就退化成「全部」
    if parts is None:
        parts = list(PART_KEYS)
    else:
        parts = [p for p in parts if p in PART_KEYS]
    if not parts:
        raise BackupError('没有选择要恢复的内容')
    merge = (mode == 'merge')
    report = {'mode': 'merge' if merge else 'replace', 'parts': parts,
              'detail': [], 'skipped': [], 'history': {}}

    cfg = current_config
    cfg.setdefault('p115', {})
    cfg['p115'].setdefault('users', {})
    cfg['p115'].setdefault('tasks', [])

    # ---- 账号 ----
    if 'users' in parts:
        users_in = (cfg_in.get('p115') or {}).get('users') or {}
        usable, skipped = {}, []
        for name, u in users_in.items():
            if not isinstance(u, dict):
                continue
            if not u.get('cookies'):
                skipped.append(name)        # 导出时剔除了 Cookie，恢复不了
                continue
            usable[name] = u
        if merge:
            for name, u in usable.items():
                cfg['p115']['users'][name] = u
        else:
            cfg['p115']['users'] = dict(usable)
        cur = (cfg_in.get('p115') or {}).get('current_user')
        if cur and cur in cfg['p115']['users']:
            cfg['p115']['current_user'] = cur
        elif cfg['p115'].get('current_user') not in cfg['p115']['users']:
            cfg['p115']['current_user'] = next(iter(cfg['p115']['users']), None)
        report['detail'].append('账号：%s %d 个' % ('合并' if merge else '恢复', len(usable)))
        if skipped:
            report['skipped'].append('有 %d 个账号在备份里没有 Cookie（导出时被排除），'
                                     '未恢复：%s' % (len(skipped), '、'.join(list(skipped)[:5])))

    # ---- 转存任务 ----
    if 'tasks' in parts:
        tasks_in = [t for t in ((cfg_in.get('p115') or {}).get('tasks') or [])
                    if isinstance(t, dict)]
        if merge:
            by_uid = {}
            for t in cfg['p115']['tasks']:
                if isinstance(t, dict) and t.get('task_uid'):
                    by_uid[t['task_uid']] = t
            added = replaced = 0
            for t in tasks_in:
                uid = t.get('task_uid')
                if uid and uid in by_uid:
                    by_uid[uid].update(t)
                    replaced += 1
                else:
                    cfg['p115']['tasks'].append(t)
                    if uid:
                        by_uid[uid] = t
                    added += 1
            report['detail'].append('转存任务：合并（新增 %d，覆盖 %d，现有 %d）'
                                    % (added, replaced, len(cfg['p115']['tasks'])))
        else:
            cfg['p115']['tasks'] = tasks_in
            report['detail'].append('转存任务：恢复 %d 个' % len(tasks_in))
        _renumber(cfg['p115']['tasks'])

    # ---- QMS 联动 ----
    if 'qms' in parts:
        cur_qms = _safe_kv(_QMS_KEY)
        if not qms_in:
            report['skipped'].append('备份里没有 QMS 配置，已跳过')
        elif merge:
            new_qms = dict(cur_qms)
            for k, v in qms_in.items():
                if k == 'links':
                    continue
                new_qms[k] = v
            links = list(cur_qms.get('links') or [])
            by_id = {str(l.get('id')): l for l in links if isinstance(l, dict)}
            added = replaced = 0
            for l in (qms_in.get('links') or []):
                if not isinstance(l, dict):
                    continue
                lid = str(l.get('id'))
                if lid and lid in by_id:
                    by_id[lid].update(l)
                    replaced += 1
                else:
                    links.append(l)
                    if lid:
                        by_id[lid] = l
                    added += 1
            new_qms['links'] = links
            report['detail'].append('QMS 联动：合并（连接新增 %d，覆盖 %d）' % (added, replaced))
            report['qms'] = new_qms
        else:
            report['qms'] = dict(qms_in)
            report['detail'].append('QMS 联动：恢复配置，连接 %d 条'
                                    % len(qms_in.get('links') or []))

    # ---- 通知 ----
    if 'notify' in parts:
        notify_in = cfg_in.get('notify')
        if isinstance(notify_in, dict):
            if merge:
                n = dict(cfg.get('notify') or {})
                n.update(notify_in)
                if 'direct_fields' in notify_in:
                    df = dict((cfg.get('notify') or {}).get('direct_fields') or {})
                    df.update(notify_in['direct_fields'] or {})
                    n['direct_fields'] = df
                cfg['notify'] = n
            else:
                cfg['notify'] = notify_in
            report['detail'].append('通知渠道：已恢复（%s）'
                                    % ('开启' if notify_in.get('enabled') else '未开启'))

    # ---- 系统设置 ----
    if 'settings' in parts:
        n = 0
        for sec in SETTING_SECTIONS:
            if isinstance(cfg_in.get(sec), dict):
                base = dict(cfg.get(sec) or {}) if merge else {}
                base.update(cfg_in[sec])
                cfg[sec] = base
                n += 1
        if n:
            report['detail'].append('系统设置：已恢复 %d 个段落' % n)

    # ---- 后台登录 ----
    if 'auth' in parts:
        auth_in = cfg_in.get('auth') or {}
        if auth_in.get('username') and auth_in.get('password'):
            base = dict(cfg.get('auth') or {}) if merge else {}
            base.update(auth_in)
            cfg['auth'] = base
            report['detail'].append('后台登录：账号已恢复为「%s」' % auth_in['username'])
        else:
            report['skipped'].append('备份里没有后台密码（导出时被排除），未恢复')

    # ---- 历史 ----
    if 'history' in parts and do_history:
        if hist_in:
            try:
                stats = restore_tables(hist_in, mode='replace' if not merge else 'merge')
                report['history'] = stats
                total = sum(stats.values())
                report['detail'].append('历史记录：写入 %d 条（%s）'
                                        % (total, '覆盖' if not merge else '合并'))
            except Exception as e:  # noqa: BLE001
                report['skipped'].append('历史记录写入失败：%s' % e)
        else:
            report['skipped'].append('备份里没有历史记录，已跳过')

    return cfg, report


def _renumber(tasks):
    """保证 order 连续不重复（合并时容易出现两个任务同号）"""
    for i, t in enumerate(tasks, 1):
        if isinstance(t, dict):
            t['order'] = i
    return tasks

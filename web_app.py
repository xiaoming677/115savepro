# -*- coding: utf-8 -*-
"""115SavePro · Flask 后端

参考 kahvia-d/BaiduAutoSave、xinyuLo/bdsavepro 的整体形态，
把存储层换成 115 网盘（p115client），并保留 QMediaSync 联动。

启动：python web_app.py      默认端口 5000
默认账号：admin / zxcvbnm
"""
import os
import secrets
import sys
import threading
import time

from flask import Flask, jsonify, request, session, send_from_directory
from loguru import logger

import notify as notifier
import qms_client
from history_db import (get_all_history, get_kv, get_qms_logs, get_task_history,
                        set_kv)
from scheduler import TaskScheduler
from storage_115 import (AVAILABLE_APPS, DEFAULT_APP, Storage115, StorageError,
                         human_size, qr_cancel, qr_new_session, qr_poll,
                         qr_selftest, _qr_sessions)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
LOG_DIR = os.path.join(BASE_DIR, 'log')
SECRET_FILE = os.path.join(CONFIG_DIR, 'secret.key')

# 版本号：更新镜像后可在页面左下角 / GET /api/version 核对
APP_VERSION = '1.1.1'

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/static')
app.config['JSON_AS_ASCII'] = False

storage = Storage115()
scheduler = TaskScheduler(storage)

# 运行中任务的实时日志：{task_uid: {'status','log':[],'started','finished','result'}}
RUN_STATE = {}
_run_lock = threading.Lock()
# 并发保护：转存是重操作，串行执行
_exec_lock = threading.Lock()


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
def _load_secret():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, 'rb') as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(SECRET_FILE, 'wb') as f:
        f.write(key)
    return key


app.secret_key = _load_secret()
app.permanent_session_lifetime = 86400 * 7


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level='INFO',
               format='<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | {message}')
    logger.add(os.path.join(LOG_DIR, 'web_app_{time:YYYY-MM-DD}.log'),
               rotation='00:00', retention='14 days', encoding='utf-8', level='DEBUG')


_setup_logging()


def ok(data=None, **extra):
    payload = {'success': True}
    if data is not None:
        payload['data'] = data
    payload.update(extra)
    return jsonify(payload)


def fail(message, code=400):
    return jsonify({'success': False, 'message': str(message)}), code


def current_username():
    return session.get('username')


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            return fail('未登录或登录已过期', 401)
        return fn(*a, **kw)
    return wrapper


def _auth_cfg():
    return storage.config.setdefault('auth', {})


@app.errorhandler(Exception)
def _on_error(e):
    logger.exception('未处理异常')
    return fail('服务器内部错误：%s' % e, 500)


@app.route('/api/version', methods=['GET'])
def api_version():
    """版本信息（不需要登录，便于更新后核对 / 健康检查）"""
    return ok({
        'version': APP_VERSION,
        'build_date': os.environ.get('BUILD_DATE', ''),
        'vcs_ref': os.environ.get('VCS_REF', ''),
    })


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    cfg = _auth_cfg()
    if str(data.get('username') or '') != str(cfg.get('username') or 'admin'):
        return fail('用户名或密码错误', 401)
    if str(data.get('password') or '') != str(cfg.get('password') or 'zxcvbnm'):
        return fail('用户名或密码错误', 401)
    session.permanent = True
    session['logged_in'] = True
    session['username'] = cfg.get('username')
    return ok({'username': cfg.get('username')})


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return ok()


@app.route('/api/auth/check', methods=['GET'])
def api_auth_check():
    return ok({'logged_in': bool(session.get('logged_in'))})


@app.route('/api/auth/password', methods=['POST'])
@login_required
def api_change_password():
    data = request.get_json(silent=True) or {}
    cfg = _auth_cfg()
    if str(data.get('old_password') or '') != str(cfg.get('password')):
        return fail('原密码不正确')
    new = str(data.get('new_password') or '').strip()
    if len(new) < 4:
        return fail('新密码至少 4 位')
    cfg['password'] = new
    storage._save_config()
    return ok()


# --------------------------------------------------------------------------
# 扫码登录 115
# --------------------------------------------------------------------------
@app.route('/api/qr/start', methods=['POST'])
@login_required
def api_qr_start():
    data = request.get_json(silent=True) or {}
    app_name = data.get('app') or DEFAULT_APP
    sid, sess = qr_new_session(app_name)
    import base64
    return ok({
        'sid': sid,
        'qrcode': 'data:image/png;base64,' + base64.b64encode(sess.qrcode_png).decode(),
        'qrcode_url': (sess.token or {}).get('qrcode') or ('https://115.com/scan/dg-' + sid),
        'expires_in': 900,
    })


@app.route('/api/qr/poll', methods=['POST'])
@login_required
def api_qr_poll():
    data = request.get_json(silent=True) or {}
    sid = data.get('sid') or ''
    try:
        sess = qr_poll(sid)
    except StorageError as e:
        return fail(str(e))
    return ok({
        'status': sess.status,
        'message': sess.message,
        'done': sess.done,
        'has_cookie': bool(sess.cookies),
        'error': sess.error,
    })


@app.route('/api/qr/commit', methods=['POST'])
@login_required
def api_qr_commit():
    data = request.get_json(silent=True) or {}
    sid = data.get('sid') or ''
    sess = _qr_sessions.get(sid)
    if not sess or not sess.cookies:
        return fail('还没有拿到登录凭据，请确认扫码已在手机上确认')
    username = (data.get('username') or '').strip()
    try:
        name = storage.add_user(username, sess.cookies, app=sess.app,
                                remark=data.get('remark') or '',
                                make_current=data.get('make_current', True))
        info = storage.check_user(name)
    except StorageError as e:
        return fail(str(e))
    qr_cancel(sid)
    return ok({'username': name, 'info': info})


@app.route('/api/qr/cancel', methods=['POST'])
@login_required
def api_qr_cancel():
    data = request.get_json(silent=True) or {}
    qr_cancel(data.get('sid') or '')
    return ok()


@app.route('/api/qr/selftest', methods=['POST'])
@login_required
def api_qr_selftest():
    """扫码登录自检：验证 token / 二维码 / 状态轮询三个接口是否都通

    不需要真的扫码。排查「参数错误」时先跑这个。
    """
    data = request.get_json(silent=True) or {}
    try:
        result = qr_selftest(data.get('app') or DEFAULT_APP)
    except Exception as e:  # noqa: BLE001
        return fail('自检失败：%s' % e)
    return ok(result)


@app.route('/api/apps', methods=['GET'])
@login_required
def api_apps():
    return ok([{'value': v, 'label': l} for v, l in AVAILABLE_APPS])


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
@app.route('/api/users', methods=['GET'])
@login_required
def api_users():
    return ok({
        'users': storage.list_users(),
        'current': storage.config['p115'].get('current_user'),
    })


@app.route('/api/user/add', methods=['POST'])
@login_required
def api_user_add():
    data = request.get_json(silent=True) or {}
    try:
        name = storage.add_user(data.get('username') or '', data.get('cookies') or '',
                                app=data.get('app') or DEFAULT_APP,
                                remark=data.get('remark') or '',
                                make_current=bool(data.get('make_current', True)))
    except StorageError as e:
        return fail(str(e))
    return ok({'username': name})


@app.route('/api/user/update', methods=['POST'])
@login_required
def api_user_update():
    data = request.get_json(silent=True) or {}
    try:
        storage.update_user(data.get('username') or '', cookies=data.get('cookies') or None,
                            remark=data.get('remark'))
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/user/switch', methods=['POST'])
@login_required
def api_user_switch():
    data = request.get_json(silent=True) or {}
    try:
        storage.switch_user(data.get('username') or '')
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/user/delete', methods=['POST'])
@login_required
def api_user_delete():
    data = request.get_json(silent=True) or {}
    try:
        storage.remove_user(data.get('username') or '')
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/user/check', methods=['POST'])
@login_required
def api_user_check():
    data = request.get_json(silent=True) or {}
    try:
        info = storage.check_user(data.get('username') or None)
    except StorageError as e:
        return fail(str(e))
    return ok(info)


@app.route('/api/user/space', methods=['GET'])
@login_required
def api_user_space():
    try:
        return ok(storage.space_info())
    except StorageError as e:
        return fail(str(e))


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------
@app.route('/api/tasks', methods=['GET'])
@login_required
def api_tasks():
    tasks = storage.list_tasks()
    for t in tasks:
        st = RUN_STATE.get(t['task_uid'])
        t['running'] = bool(st and not st.get('finished'))
        t['run_log_lines'] = len(st.get('log') or []) if st else 0
    return ok(tasks)


@app.route('/api/tasks/running', methods=['GET'])
@login_required
def api_tasks_running():
    out = []
    for uid, st in RUN_STATE.items():
        if not st.get('finished'):
            out.append({'task_uid': uid, 'started': st.get('started'),
                        'last_log': (st.get('log') or [''])[-1]})
    return ok(out)


@app.route('/api/task/status/<task_uid>', methods=['GET'])
@login_required
def api_task_status(task_uid):
    st = RUN_STATE.get(task_uid)
    if not st:
        return ok({'exists': False})
    return ok({'exists': True, 'status': st.get('status'), 'log': st.get('log') or [],
               'started': st.get('started'), 'finished': st.get('finished'),
               'result': st.get('result')})


@app.route('/api/task/add', methods=['POST'])
@login_required
def api_task_add():
    data = request.get_json(silent=True) or {}
    try:
        task = storage.add_task(
            url=data.get('url') or '', save_dir=data.get('save_dir') or '',
            pwd=data.get('pwd') or None, name=data.get('name') or None,
            cron=data.get('cron') or None, category=data.get('category') or None,
            regex_pattern=data.get('regex_pattern') or None,
            regex_replace=data.get('regex_replace') or None,
            compare_path=data.get('compare_path') or '',
            dedupe_mode=data.get('dedupe_mode') or 'name',
            enabled=data.get('enabled', True),
            include_subdirs=data.get('include_subdirs', True),
            transfer_file_ids=data.get('transfer_file_ids') or [],
            exclude_files=data.get('exclude_files') or [],
        )
    except StorageError as e:
        return fail(str(e))
    scheduler.sync_jobs()
    return ok(task)


@app.route('/api/task/update', methods=['POST'])
@login_required
def api_task_update():
    data = request.get_json(silent=True) or {}
    task_uid = data.get('task_uid') or ''
    try:
        task = storage.update_task(task_uid, data.get('data') or {})
    except StorageError as e:
        return fail(str(e))
    scheduler.sync_jobs()
    return ok(task)


@app.route('/api/task/delete', methods=['POST'])
@login_required
def api_task_delete():
    data = request.get_json(silent=True) or {}
    try:
        storage.remove_task(data.get('task_uid') or '')
    except StorageError as e:
        return fail(str(e))
    scheduler.sync_jobs()
    return ok()


@app.route('/api/tasks/batch-delete', methods=['POST'])
@login_required
def api_tasks_batch_delete():
    data = request.get_json(silent=True) or {}
    storage.remove_tasks(data.get('task_uids') or [])
    scheduler.sync_jobs()
    return ok()


@app.route('/api/task/toggle', methods=['POST'])
@login_required
def api_task_toggle():
    data = request.get_json(silent=True) or {}
    task_uid = data.get('task_uid') or ''
    task = storage.get_task_by_uid(task_uid)
    if not task:
        return fail('任务不存在')
    enabled = data.get('enabled')
    if enabled is None:
        enabled = not task.get('enabled', True)
    storage.update_task(task_uid, {'enabled': bool(enabled)})
    scheduler.sync_jobs()
    return ok({'enabled': bool(enabled)})


@app.route('/api/task/reorder', methods=['POST'])
@login_required
def api_task_reorder():
    data = request.get_json(silent=True) or {}
    try:
        storage.reorder_task(data.get('task_uid') or '', data.get('order') or 1)
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/task/history/<task_uid>', methods=['GET'])
@login_required
def api_task_history(task_uid):
    return ok(get_task_history(task_uid=task_uid, limit=int(request.args.get('limit', 10))))


@app.route('/api/history', methods=['GET'])
@login_required
def api_history():
    return ok(get_all_history(limit=int(request.args.get('limit', 100))))


def _run_task_async(task, source='手动'):
    task_uid = task['task_uid']
    with _run_lock:
        RUN_STATE[task_uid] = {'status': 'running', 'log': [], 'started': time.strftime('%H:%M:%S'),
                               'finished': False, 'result': None}
    try:
        with _exec_lock:
            result = scheduler.run_task(task, source=source)
        record = result.get('record') or {}
        with _run_lock:
            RUN_STATE[task_uid].update({
                'status': 'success' if result.get('success') else 'failed',
                'log': record.get('log') or [],
                'finished': True,
                'result': {
                    'success': result.get('success'),
                    'message': result.get('message'),
                    'file_count': result.get('file_count'),
                    'new_items': result.get('new_items'),
                },
            })
    except Exception as e:  # noqa: BLE001
        logger.exception('异步执行任务失败')
        with _run_lock:
            RUN_STATE[task_uid].update({'status': 'failed', 'finished': True,
                                        'result': {'success': False, 'message': str(e)}})


@app.route('/api/task/execute', methods=['POST'])
@login_required
def api_task_execute():
    data = request.get_json(silent=True) or {}
    task = storage.resolve_task(task_uid=data.get('task_uid'), order=data.get('order'),
                                url=data.get('url'))
    if not task:
        return fail('任务不存在')
    t = threading.Thread(target=_run_task_async, args=(task, '手动'), daemon=True)
    t.start()
    return ok({'task_uid': task['task_uid'], 'message': '已开始执行，可查看实时日志'})


@app.route('/api/tasks/execute-all', methods=['POST'])
@login_required
def api_tasks_execute_all():
    data = request.get_json(silent=True) or {}
    tasks = storage.list_tasks()
    if not data.get('include_disabled'):
        tasks = [t for t in tasks if t.get('enabled', True)]
    if not tasks:
        return fail('没有可执行的任务')

    def worker():
        for task in tasks:
            _run_task_async(task, '手动(全部)')

    threading.Thread(target=worker, daemon=True).start()
    return ok({'count': len(tasks), 'message': '已开始执行 %d 个任务' % len(tasks)})


# --------------------------------------------------------------------------
# 分享浏览（选择转存文件夹）
# --------------------------------------------------------------------------
@app.route('/api/share/browse', methods=['POST'])
@login_required
def api_share_browse():
    data = request.get_json(silent=True) or {}
    try:
        return ok(storage.share_browse(data.get('url') or '', pwd=data.get('pwd'),
                                       cid=int(data.get('cid') or 0)))
    except StorageError as e:
        return fail(str(e))


# --------------------------------------------------------------------------
# 目录选择器
# --------------------------------------------------------------------------
@app.route('/api/dir/list', methods=['GET'])
@login_required
def api_dir_list():
    parent = request.args.get('cid') or 0
    try:
        if str(parent) in ('root', '', '0'):
            cid = 0
        elif str(parent).startswith('/'):
            cid = storage.dir_id(parent)
        else:
            cid = int(parent)
        return ok({'cid': cid, 'items': storage.path_tree(cid)})
    except StorageError as e:
        return fail(str(e))


@app.route('/api/dir/resolve', methods=['POST'])
@login_required
def api_dir_resolve():
    data = request.get_json(silent=True) or {}
    try:
        cid = storage.ensure_dir(data.get('path') or '/')
    except StorageError as e:
        return fail(str(e))
    return ok({'path': storage.normalize_path(data.get('path') or '/'), 'cid': cid})


# --------------------------------------------------------------------------
# 离线下载
# --------------------------------------------------------------------------
@app.route('/api/offline/list', methods=['GET'])
@login_required
def api_offline_list():
    page = int(request.args.get('page', 1))
    stat = request.args.get('stat')
    try:
        return ok(storage.offline_list(page=page, page_size=int(request.args.get('page_size', 50)),
                                       stat=int(stat) if stat else None))
    except StorageError as e:
        return fail(str(e))


@app.route('/api/offline/add', methods=['POST'])
@login_required
def api_offline_add():
    data = request.get_json(silent=True) or {}
    try:
        result = storage.offline_add(data.get('urls') or '', save_dir=data.get('save_dir') or None)
    except StorageError as e:
        return fail(str(e))
    return ok({'submitted': result.get('submitted')})


@app.route('/api/offline/delete', methods=['POST'])
@login_required
def api_offline_delete():
    data = request.get_json(silent=True) or {}
    try:
        storage.offline_delete(data.get('hashes') or [])
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/offline/clear', methods=['POST'])
@login_required
def api_offline_clear():
    data = request.get_json(silent=True) or {}
    try:
        storage.offline_clear(flag=data.get('flag') or 0)
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/offline/restart', methods=['POST'])
@login_required
def api_offline_restart():
    data = request.get_json(silent=True) or {}
    try:
        storage.offline_restart(data.get('hashes') or [])
    except StorageError as e:
        return fail(str(e))
    return ok()


# --------------------------------------------------------------------------
# 文件管理
# --------------------------------------------------------------------------
@app.route('/api/files/list', methods=['GET'])
@login_required
def api_files_list():
    try:
        cid = int(request.args.get('cid') or 0)
        page = storage.list_dir(cid, offset=int(request.args.get('offset', 0)),
                                limit=int(request.args.get('limit', 200)))
        return ok(page)
    except (StorageError, ValueError) as e:
        return fail(str(e))


@app.route('/api/files/mkdir', methods=['POST'])
@login_required
def api_files_mkdir():
    data = request.get_json(silent=True) or {}
    try:
        storage.fs_mkdir(data.get('cid') or 0, data.get('name') or '')
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/files/rename', methods=['POST'])
@login_required
def api_files_rename():
    data = request.get_json(silent=True) or {}
    try:
        storage.fs_rename(data.get('fid'), data.get('name') or '')
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/files/move', methods=['POST'])
@login_required
def api_files_move():
    data = request.get_json(silent=True) or {}
    try:
        storage.fs_move(data.get('fids') or [], data.get('target_cid') or 0)
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/files/copy', methods=['POST'])
@login_required
def api_files_copy():
    data = request.get_json(silent=True) or {}
    try:
        storage.fs_copy(data.get('fids') or [], data.get('target_cid') or 0)
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_files_delete():
    data = request.get_json(silent=True) or {}
    try:
        storage.fs_delete(data.get('fids') or [])
    except StorageError as e:
        return fail(str(e))
    return ok()


@app.route('/api/files/search', methods=['POST'])
@login_required
def api_files_search():
    data = request.get_json(silent=True) or {}
    try:
        return ok(storage.fs_search(data.get('keyword') or '', cid=int(data.get('cid') or 0)))
    except StorageError as e:
        return fail(str(e))


@app.route('/api/files/share', methods=['POST'])
@login_required
def api_files_share():
    data = request.get_json(silent=True) or {}
    try:
        return ok(storage.share_create(data.get('fids') or [],
                                       receive_code=data.get('receive_code') or None,
                                       days=data.get('days')))
    except StorageError as e:
        return fail(str(e))


# --------------------------------------------------------------------------
# 配置 / 通知
# --------------------------------------------------------------------------
@app.route('/api/config', methods=['GET'])
@login_required
def api_config():
    cfg = {k: v for k, v in storage.config.items() if k != 'auth'}
    cfg['auth'] = {'username': _auth_cfg().get('username'),
                   'session_timeout': _auth_cfg().get('session_timeout')}
    cfg['notify_fields'] = notifier.FIELD_META
    return ok(cfg)


@app.route('/api/config/update', methods=['POST'])
@login_required
def api_config_update():
    data = request.get_json(silent=True) or {}
    for section in ('cron', 'notify', 'scheduler', 'quota_alert', 'regex',
                    'file_operations', 'offline'):
        if section in data and isinstance(data[section], dict):
            storage.config.setdefault(section, {})
            if section == 'notify' and 'direct_fields' in data[section]:
                df = storage.config[section].setdefault('direct_fields', {})
                df.update(data[section]['direct_fields'] or {})
                rest = {k: v for k, v in data[section].items() if k != 'direct_fields'}
                storage.config[section].update(rest)
            else:
                storage.config[section].update(data[section])
    storage._save_config()
    scheduler.sync_jobs()
    scheduler._add_quota_job()
    scheduler._add_offline_watch_job()
    return ok()


@app.route('/api/notify/test', methods=['POST'])
@login_required
def api_notify_test():
    data = request.get_json(silent=True) or {}
    cfg = storage.config.get('notify') or {}
    if data.get('direct_fields'):
        cfg = dict(cfg)
        cfg['direct_fields'] = {**(cfg.get('direct_fields') or {}), **(data['direct_fields'] or {})}
    results = notifier.send('115SavePro 测试通知', '如果你收到这条消息，说明通知配置正常。',
                            cfg, force=True)
    return ok({'results': results})


@app.route('/api/scheduler/jobs', methods=['GET'])
@login_required
def api_scheduler_jobs():
    return ok(scheduler.get_jobs())


@app.route('/api/scheduler/reload', methods=['POST'])
@login_required
def api_scheduler_reload():
    storage.reload()
    scheduler.sync_jobs()
    return ok(scheduler.get_jobs())


# --------------------------------------------------------------------------
# QMediaSync
# --------------------------------------------------------------------------
@app.route('/api/qms/config', methods=['GET'])
@login_required
def api_qms_config():
    cfg = qms_client.load_cfg()
    norm = qms_client.normalize_config(cfg)
    norm['has_api_key'] = bool(norm.get('api_key'))
    norm['has_password'] = bool(norm.get('password'))
    norm.pop('api_key', None)
    norm.pop('password', None)
    return ok({'config': norm, 'links': qms_client.get_links(cfg),
               'last_trigger_at': cfg.get('last_trigger_at'),
               'last_trigger_result': cfg.get('last_trigger_result'),
               'last_trigger_task': cfg.get('last_trigger_task'),
               'last_trigger_ok': cfg.get('last_trigger_ok')})


@app.route('/api/qms/config/save', methods=['POST'])
@login_required
def api_qms_config_save():
    data = request.get_json(silent=True) or {}
    saved = qms_client.load_cfg()
    merged = qms_client.merge_cfg(saved, data)
    qms_client.save_cfg(merged)
    return ok()


@app.route('/api/qms/test', methods=['POST'])
@login_required
def api_qms_test():
    data = request.get_json(silent=True) or {}
    saved = qms_client.load_cfg()
    cfg = qms_client.merge_cfg(saved, data)
    try:
        info = qms_client.QmsClient(cfg).test()
    except qms_client.QmsError as e:
        return fail(str(e))
    return ok(info)


@app.route('/api/qms/pathes', methods=['POST'])
@login_required
def api_qms_pathes():
    data = request.get_json(silent=True) or {}
    saved = qms_client.load_cfg()
    cfg = qms_client.merge_cfg(saved, data)
    try:
        return ok(qms_client.QmsClient(cfg).list_scrape_paths())
    except qms_client.QmsError as e:
        return fail(str(e))


@app.route('/api/qms/link/add', methods=['POST'])
@login_required
def api_qms_link_add():
    data = request.get_json(silent=True) or {}
    cfg = qms_client.load_cfg()
    links = cfg.get('links') or []
    scope = str(data.get('scope') or 'task')
    link = {
        'id': qms_client.new_link_id(),
        'scope': scope,
        'task_uid': str(data.get('task_uid') or ''),
        'task_name': str(data.get('task_name') or ''),
        'qms_id': data.get('qms_id'),
        'qms_path': str(data.get('qms_path') or ''),
        'qms_media_type': str(data.get('qms_media_type') or ''),
        'enabled': True,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    links.append(link)
    cfg['links'] = links
    qms_client.save_cfg(cfg)
    return ok(link)


@app.route('/api/qms/link/delete', methods=['POST'])
@login_required
def api_qms_link_delete():
    data = request.get_json(silent=True) or {}
    cfg = qms_client.load_cfg()
    lid = str(data.get('id') or '')
    cfg['links'] = [l for l in (cfg.get('links') or []) if str(l.get('id')) != lid]
    qms_client.save_cfg(cfg)
    return ok()


@app.route('/api/qms/link/toggle', methods=['POST'])
@login_required
def api_qms_link_toggle():
    data = request.get_json(silent=True) or {}
    cfg = qms_client.load_cfg()
    lid = str(data.get('id') or '')
    for l in (cfg.get('links') or []):
        if str(l.get('id')) == lid:
            l['enabled'] = not l.get('enabled', True)
    qms_client.save_cfg(cfg)
    return ok()


@app.route('/api/qms/trigger', methods=['POST'])
@login_required
def api_qms_trigger():
    data = request.get_json(silent=True) or {}
    cfg = qms_client.load_cfg()
    lid = str(data.get('id') or '')
    link = next((l for l in qms_client.get_links(cfg) if l['id'] == lid), None)
    if not link:
        return fail('连接不存在')
    result = qms_client.trigger_link(link, source='manual')
    if not result.get('ok'):
        return fail(result.get('message') or '触发失败')
    return ok(result)


@app.route('/api/qms/trigger-all', methods=['POST'])
@login_required
def api_qms_trigger_all():
    cfg = qms_client.load_cfg()
    links = [l for l in qms_client.get_links(cfg) if str(l.get('qms_id') or '').isdigit()]
    if not links:
        return fail('没有可触发的连接')
    results = [qms_client.trigger_link(l, source='manual') for l in links]
    return ok({'results': results, 'summary': qms_client.build_summary(results)})


@app.route('/api/qms/logs', methods=['GET'])
@login_required
def api_qms_logs():
    lid = request.args.get('id') or ''
    return ok(get_qms_logs(lid, limit=int(request.args.get('limit', 30))))


# --------------------------------------------------------------------------
# 前端
# --------------------------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory(os.path.join(BASE_DIR, 'templates'), 'index.html')


@app.route('/favicon.ico')
def favicon():
    return ('', 204)


def main():
    port = int(os.environ.get('PORT') or 5000)
    host = os.environ.get('HOST') or '0.0.0.0'
    logger.info('115SavePro v%s 启动中…  http://%s:%s'
                % (APP_VERSION, '127.0.0.1' if host == '0.0.0.0' else host, port))
    try:
        scheduler.start()
    except Exception as e:  # noqa: BLE001
        logger.error('调度器启动失败（不影响网页使用）：%s' % e)
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()

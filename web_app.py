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
import backup
import qms_client
import updater
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
APP_VERSION = '1.7.2'

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


def _truthy(value, default=False):
    """把前端传来的 0/1、true/false、'on' 统一成布尔值

    区分「没传」和「传了 false」：没传就用默认值。
    """
    if value is None or value == '':
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


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
        'app': sess.app,
        'app_used': sess.app_used,
        'tried_apps': sess.tried_apps,
        'debug': sess.debug,
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
        name = storage.add_user(username, sess.cookies,
                                app=sess.app_used or sess.app,
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
    """扫码登录自检：验证 token / 二维码 / 状态轮询 / 时间同步 四项

    不需要真的扫码。排查「参数错误」时先跑这个 —— 若四项全绿，
    说明代码与网络都没问题，问题在 115 侧（风控或账号状态）。
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
    # 保存即执行（用户要求：不用再手动点一次「执行」）
    started = _maybe_auto_run(task, data)
    resp = dict(task)
    resp['auto_started'] = started
    return ok(resp)


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
    started = _maybe_auto_run(task, data)
    resp = dict(task)
    resp['auto_started'] = started
    return ok(resp)


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


def _maybe_auto_run(task, req_data=None):
    """保存任务后自动执行一次（可在「系统设置」里关掉）

    为什么要做成可配置：批量建任务、或只想先改配置稍后再跑的时候，
    每次保存都真跑一遍会白白消耗 115 的转存配额。

    优先级：请求里的 auto_run > 配置 task.auto_run_on_save > 默认开启。
    返回是否真的触发了执行。
    """
    req = req_data or {}
    auto = req.get('auto_run')
    if auto is None:
        auto = (storage.config.get('task') or {}).get('auto_run_on_save', True)
    if not auto:
        return False
    if not task or not task.get('task_uid'):
        return False
    if not task.get('enabled', True):
        return False                      # 停用的任务不自动跑
    with _run_lock:
        if (RUN_STATE.get(task['task_uid']) or {}).get('status') == 'running':
            return False                  # 已经在跑，别重复触发
    threading.Thread(target=_run_task_async,
                     args=(task, '保存后自动执行'), daemon=True).start()
    return True


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
@app.route('/api/dir/debug', methods=['GET'])
@login_required
def api_dir_debug():
    """目录诊断：把 115 返回的原始数据原样拿出来

    用于排查「看不到目录 / 进不去子目录」这类问题 —— 这类问题往往出在
    115 返回的字段形态上，光看归一化后的结果猜不出来。

    参数 cid 同 /api/dir/list；会分别用「带 nf=1（只要目录）」和
    「不带 nf（目录+文件）」各请求一次，方便对照。
    """
    cid_arg = request.args.get('cid') or 0
    try:
        if str(cid_arg) in ('root', '', '0'):
            cid = 0
        elif str(cid_arg).startswith('/'):
            cid = storage.dir_id(cid_arg)
        else:
            cid = int(cid_arg)
    except (TypeError, ValueError):
        return fail('目录 id 不合法：%s' % cid_arg)
    except StorageError as e:
        return fail(str(e))

    capture = []
    out = {'cid': cid, 'step': '调用 115 的 /files 接口两次'}
    try:
        client = storage.current_client()
    except StorageError as e:
        return fail(str(e))
    res = {
        '账号': '',
        'cid': cid,
        '请求记录': capture,
        '归一化结果': None,
        '提示': ('把这一整段 JSON 发给开发者即可定位。'
                 'raw_sample 是 115 原样返回的前 3 条数据，'
                 '重点看目录条目里到底有没有 fid / pid / fc 这些字段。'),
    }
    try:
        tree = storage.path_tree(cid, client=client, with_raw=True, capture=capture)
        res['归一化结果'] = tree
    except StorageError as e:
        res['归一化结果'] = {'error': str(e)}
    res['账号'] = storage.config['p115'].get('current_user') or ''
    res['版本'] = APP_VERSION
    return ok(res)


@app.route('/api/dir/list', methods=['GET'])
@login_required
def api_dir_list():
    """列出某层子目录，供路径选择器使用

    参数：
      cid  目录 id（默认 0 = 根目录）；也接受 root / 空
      path 直接传路径（与 cid 二选一，path 优先）
      raw  1 = 附带 115 的原始条目，用于排查「看不到目录」
    """
    cid_arg = request.args.get('cid') or 0
    want_raw = (request.args.get('raw') or '') in ('1', 'true', 'yes')
    path = (request.args.get('path') or '').strip()
    try:
        cid = 0
        if path:
            chain = storage.dir_chain(path)
            cid = chain[-1]['cid'] if chain else 0
        elif str(cid_arg) in ('root', '', '0'):
            cid = 0
        elif str(cid_arg).startswith('/'):
            cid = storage.dir_id(cid_arg)
        else:
            try:
                cid = int(cid_arg)
            except (TypeError, ValueError):
                return fail('目录 id 不合法：%s' % cid_arg)
        data = storage.path_tree(cid, with_raw=want_raw)
        data['cid'] = cid
        return ok(data)
    except StorageError as e:
        return fail(str(e))


@app.route('/api/dir/chain', methods=['GET', 'POST'])
@login_required
def api_dir_chain():
    """把路径解析成层级链，让选择器打开时能定位到当前填的路径"""
    data = request.get_json(silent=True) if request.method == 'POST' else {}
    path = (data or {}).get('path') or request.args.get('path') or '/'
    try:
        return ok({'path': storage.normalize_path(path), 'chain': storage.dir_chain(path)})
    except StorageError as e:
        return fail(str(e))


@app.route('/api/dir/create', methods=['POST'])
@login_required
def api_dir_create():
    """在选择器里新建目录（115 里建好之后才能选中）"""
    data = request.get_json(silent=True) or {}
    parent = data.get('cid')
    name = (data.get('name') or '').strip()
    if not name:
        return fail('请输入目录名')
    for ch in '<>':
        if ch in name:
            return fail('目录名不能包含 %s' % ch)
    if '/' in name or '\\' in name:
        return fail('目录名不能包含斜杠')
    try:
        parent_cid = parent if parent is not None else 0
        storage.fs_mkdir(parent_cid, name)
        # 建完之后回查一次拿到新目录的 cid，前端可以直接进去
        new_cid = None
        try:
            for it in storage.list_dir(parent_cid, limit=storage.MAX_PAGE,
                                       only_dir=True)['items']:
                if it.get('is_dir') and str(it.get('name')) == name:
                    new_cid = it['id']
                    break
        except StorageError:
            pass
        return ok({'cid': new_cid, 'name': name})
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
                    'file_operations', 'offline', 'update', 'task'):
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
               'wait_modes': [{'value': v, 'label': t} for v, t in qms_client.WAIT_MODES],
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


@app.route('/api/qms/sync-paths', methods=['POST'])
@login_required
def api_qms_sync_paths():
    """列 QMS 的同步路径 —— 用于「先生成 strm」下拉"""
    data = request.get_json(silent=True) or {}
    cfg = qms_client.merge_cfg(qms_client.load_cfg(), data)
    try:
        return ok(qms_client.QmsClient(cfg).list_sync_paths())
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
        # ---- 先生成 strm，再刮削 ----
        'sync_path_id': data.get('sync_path_id') or None,
        'sync_path_name': str(data.get('sync_path_name') or ''),
        'wait_mode': str(data.get('wait_mode') or qms_client.DEFAULT_WAIT_MODE),
        'wait_seconds': int(data.get('wait_seconds') or qms_client.DEFAULT_WAIT_SECONDS),
        'wait_timeout': int(data.get('wait_timeout') or qms_client.DEFAULT_WAIT_TIMEOUT),
    }
    links.append(link)
    cfg['links'] = links
    qms_client.save_cfg(cfg)
    return ok(qms_client.get_links({'links': [link]})[0])


@app.route('/api/qms/link/update', methods=['POST'])
@login_required
def api_qms_link_update():
    """更新一条连接的配置（改绑同步路径 / 调整等待策略 / 换刮削任务）"""
    data = request.get_json(silent=True) or {}
    lid = str(data.get('id') or '')
    if not lid:
        return fail('缺少连接 id')
    cfg = qms_client.load_cfg()
    target = None
    for l in (cfg.get('links') or []):
        if str(l.get('id')) == lid:
            target = l
            break
    if target is None:
        return fail('连接不存在')

    if 'qms_id' in data:
        target['qms_id'] = data.get('qms_id')
    if 'qms_path' in data:
        target['qms_path'] = str(data.get('qms_path') or '')
    if 'sync_path_id' in data:
        target['sync_path_id'] = data.get('sync_path_id') or None
    if 'sync_path_name' in data:
        target['sync_path_name'] = str(data.get('sync_path_name') or '')
    if 'wait_mode' in data:
        mode = str(data.get('wait_mode') or qms_client.DEFAULT_WAIT_MODE)
        target['wait_mode'] = mode if mode in ('poll', 'delay', 'none') else qms_client.DEFAULT_WAIT_MODE
    if 'wait_seconds' in data:
        target['wait_seconds'] = max(int(data.get('wait_seconds') or qms_client.DEFAULT_WAIT_SECONDS), 0)
    if 'wait_timeout' in data:
        target['wait_timeout'] = max(int(data.get('wait_timeout') or qms_client.DEFAULT_WAIT_TIMEOUT), 30)

    qms_client.save_cfg(cfg)
    return ok(qms_client.get_links({'links': [target]})[0])


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
# 版本检查与一键更新
# --------------------------------------------------------------------------
def _self_update_enabled():
    return bool((storage.config.get('update') or {}).get('allow_self_update', True))


@app.route('/api/update/check', methods=['GET'])
@login_required
def api_update_check():
    """检查是否有新版本（force=1 跳过 5 分钟缓存）"""
    force = (request.args.get('force') or '') in ('1', 'true', 'yes')
    return ok(updater.check_update(APP_VERSION, use_cache=not force))


@app.route('/api/update/env', methods=['GET'])
@login_required
def api_update_env():
    """一键更新的可用性 —— 不可用时把「缺什么、怎么补」讲清楚"""
    supported, reason, info = updater.update_capability()
    if supported and not _self_update_enabled():
        supported, reason = False, '已在设置里关闭「允许一键更新」'
    return ok({
        'version': APP_VERSION,
        'supported': supported,
        'reason': reason,
        'container': info,
        'assistant_image': updater.ASSISTANT_IMAGE,
        'socket': updater.DOCKER_SOCKET,
        # 给前端做诊断用：容器内到底有没有这个 socket、有没有挂在别处
        'socket_exists': os.path.exists(updater.DOCKER_SOCKET),
        'socket_found': updater.find_docker_sockets(),
        'running_as_root': (os.getuid() == 0) if hasattr(os, 'getuid') else None,
        'enabled': _self_update_enabled(),
    })


@app.route('/api/update/apply', methods=['POST'])
@login_required
def api_update_apply():
    """一键更新：启动助手容器去拉新镜像并重建本容器

    本请求会立刻返回 —— 几秒后本容器会被助手停掉重建，页面会短暂断线。
    """
    if not _self_update_enabled():
        return fail('已在设置里关闭「允许一键更新」')
    started, msg = updater.run_self_update()
    return ok({'started': True, 'message': msg}) if started else fail(msg)


@app.route('/api/update/log', methods=['GET'])
@login_required
def api_update_log():
    """上次更新的输出（由助手容器写入 config/update.log）"""
    return ok({'log': updater.read_update_log()})


@app.route('/api/update/log/clear', methods=['POST'])
@login_required
def api_update_log_clear():
    updater.clear_update_log()
    return ok()


# --------------------------------------------------------------------------
# 备份与恢复
# --------------------------------------------------------------------------
def _apply_backup_payload(payload, mode='replace', parts=None, save_current=True):
    """把备份应用到当前状态（共用逻辑：本地备份恢复 / 上传文件恢复）

    save_current=True 时先自动把当前状态存成一份 before-restore 备份，
    拿错文件也能退回来。
    """
    payload = backup.validate(payload)
    # 显式传了空列表/全是无效项 → 直接拒绝，别白存一份安全备份
    if parts is not None and not [p for p in parts if p in backup.PART_KEYS]:
        raise backup.BackupError('没有选择要恢复的内容')
    safety = ''
    if save_current:
        try:
            cur = backup.build_payload(storage.config, include_cookies=True,
                                      include_auth=True, include_history=False)
            safety = backup.save_local(cur, tag='before-restore')
        except Exception as e:  # noqa: BLE001
            logger.warning('恢复前自动存档失败：%s' % e)

    # 备份里如果有 QMS 配置，先取出（apply_payload 会给回来）
    qms_before = None
    try:
        qms_before = qms_client.get_links(qms_client.load_cfg())
    except Exception:  # noqa: BLE001
        pass

    new_cfg, report = backup.apply_payload(
        storage.config, payload,
        mode=mode, parts=parts,
    )

    # 写回配置 + 让内存状态、客户端缓存、定时任务全部重载
    try:
        storage.config = new_cfg
        storage._save_config()
        storage.reload()
    except Exception as e:  # noqa: BLE001
        raise StorageError('配置写回失败：%s' % e)

    if 'qms' in (list(backup.PART_KEYS) if parts is None else parts) and report.get('qms'):
        qms_client.save_cfg(report['qms'])
        report['detail'].append('QMS 联动配置已写入')

    scheduler.sync_jobs()
    scheduler._add_quota_job()
    scheduler._add_offline_watch_job()

    report['safety_backup'] = safety
    report['version'] = APP_VERSION
    report['qms_links'] = len(qms_client.get_links(qms_client.load_cfg()))
    report['qms_links_before'] = len(qms_before or [])
    return report


@app.route('/api/backup/parts', methods=['GET'])
@login_required
def api_backup_parts():
    """可恢复的部件清单（前端渲染勾选框用）"""
    return ok({
        'parts': [{'key': k, 'label': v} for k, v in backup.PARTS],
        'max_local': backup.MAX_LOCAL,
        'dir': 'config/backups',
    })


@app.route('/api/backup/export', methods=['GET', 'POST'])
@login_required
def api_backup_export():
    """生成备份。save=1 时同时在服务器上留一份"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
    else:
        data = request.args
    inc_cookies = _truthy(data.get('cookies'), True)
    inc_auth = _truthy(data.get('auth'), True)
    inc_history = _truthy(data.get('history'), True)
    save = _truthy(data.get('save'), False)

    payload = backup.build_payload(storage.config,
                                   include_cookies=inc_cookies,
                                   include_auth=inc_auth,
                                   include_history=inc_history)
    name = ''
    if save:
        try:
            name = backup.save_local(payload, tag='manual')
        except Exception as e:  # noqa: BLE001
            return fail('保存到服务器失败：%s' % e)
    return ok({'payload': payload, 'saved_name': name,
               'summary': payload.get('summary'),
               'filename': '115savepro-backup-%s.json' % time.strftime('%Y%m%d-%H%M%S')})


@app.route('/api/backup/preview', methods=['POST'])
@login_required
def api_backup_preview():
    """预览一份备份（上传的文本或服务器上的文件）"""
    data = request.get_json(silent=True) or {}
    try:
        if data.get('content'):
            payload = backup.parse_text(data['content'])
        elif data.get('name'):
            payload = backup.load_local(data['name'])
        else:
            return fail('请提供备份内容或备份文件名')
        return ok(backup.preview(payload))
    except backup.BackupError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception('预览备份失败')
        return fail('无法解析这份备份：%s' % e)


@app.route('/api/backup/import', methods=['POST'])
@login_required
def api_backup_import():
    """从上传的内容恢复"""
    data = request.get_json(silent=True) or {}
    mode = 'merge' if str(data.get('mode') or '').lower() == 'merge' else 'replace'
    parts = data.get('parts')
    if parts is not None and not isinstance(parts, list):
        parts = None
    try:
        payload = backup.parse_text(data.get('content') or '')
        report = _apply_backup_payload(payload, mode=mode, parts=parts)
        return ok(report)
    except backup.BackupError as e:
        return fail(str(e))
    except StorageError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception('恢复备份失败')
        return fail('恢复失败：%s' % e)


@app.route('/api/backup/list', methods=['GET'])
@login_required
def api_backup_list():
    return ok({'items': backup.list_local(), 'max_local': backup.MAX_LOCAL,
               'dir': 'config/backups'})


@app.route('/api/backup/save', methods=['POST'])
@login_required
def api_backup_save():
    """在服务器上生成一份备份（不下载）"""
    data = request.get_json(silent=True) or {}
    payload = backup.build_payload(storage.config,
                                   include_cookies=_truthy(data.get('cookies'), True),
                                   include_auth=_truthy(data.get('auth'), True),
                                   include_history=_truthy(data.get('history'), True))
    try:
        name = backup.save_local(payload, tag='manual')
    except Exception as e:  # noqa: BLE001
        return fail('保存失败：%s' % e)
    return ok({'name': name, 'summary': payload.get('summary')})


@app.route('/api/backup/restore', methods=['POST'])
@login_required
def api_backup_restore():
    """用服务器上的某个备份恢复"""
    data = request.get_json(silent=True) or {}
    mode = 'merge' if str(data.get('mode') or '').lower() == 'merge' else 'replace'
    parts = data.get('parts')
    if parts is not None and not isinstance(parts, list):
        parts = None
    try:
        payload = backup.load_local(data.get('name') or '')
        report = _apply_backup_payload(payload, mode=mode, parts=parts)
        return ok(report)
    except backup.BackupError as e:
        return fail(str(e))
    except StorageError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception('从本地备份恢复失败')
        return fail('恢复失败：%s' % e)


@app.route('/api/backup/download/<path:name>', methods=['GET'])
@login_required
def api_backup_download(name):
    """下载服务器上的某份备份"""
    try:
        backup.load_local(name)          # 先校验文件名与可读性
        return send_from_directory(backup.BACKUP_DIR, name, as_attachment=True)
    except backup.BackupError as e:
        return fail(str(e), 404)


@app.route('/api/backup/delete', methods=['POST'])
@login_required
def api_backup_delete():
    data = request.get_json(silent=True) or {}
    try:
        backup.delete_local(data.get('name') or '')
        return ok({'items': backup.list_local()})
    except backup.BackupError as e:
        return fail(str(e))


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

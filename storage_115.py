# -*- coding: utf-8 -*-
"""115 网盘存储层

职责：
- 配置读写（config/config.json）
- 多账号管理（扫码登录 / Cookie 导入）、客户端缓存
- 转存任务 CRUD
- 路径 <-> 目录 id 解析、目录罗列
- 分享链接解析、分享目录浏览、转存（含去重）
- 离线下载（磁力 / ed2k / HTTP）
- 文件管理（新建 / 重命名 / 移动 / 复制 / 删除 / 搜索 / 生成分享）
- 空间信息

依赖 p115client（ChenyangGao）。所有网络调用都会把 p115client 的异常
翻译成人类可读的中文提示。
"""
import json
import hashlib
import os
import re
import threading
import time

from loguru import logger

from p115client import P115Client, P115ShareFileSystem, P115OSError, check_response
from p115client import tool as p115tool
from p115client.tool import normalize_attr_simple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
TEMPLATE_PATH = os.path.join(CONFIG_DIR, 'config.template.json')

# 允许绑定 cookie 的设备（扫码后即以此设备身份登录）
#
# 顺序 = 界面下拉框的展示顺序，第一个是默认值。
#
# 为什么默认改成 alipaymini（而不是上一版的 web）：
#   参考 xiaoya-alist 修复 115 扫码的 commit（a25334b），115 的扫码设备
#   清单里是 windows/mac/linux/wechatmini/alipaymini 这类，**web 不在清单内**；
#   且 p115client 文档明确警告 app="web" 最容易触发「IP登录异常」，
#   该风控要到次日零点才解禁。所以把 web 降到最末，默认用 alipaymini。
AVAILABLE_APPS = [
    ('alipaymini', '115生活_支付宝小程序（推荐）'),
    ('wechatmini', '115生活_微信小程序'),
    ('android', '115生活_安卓端'),
    ('115android', '115_安卓端'),
    ('ios', '115生活_苹果端'),
    ('ipad', '115生活_苹果平板端'),
    ('os_windows', '115生活_Windows端'),
    ('os_linux', '115生活_Linux端'),
    ('harmony', '115_鸿蒙端'),
    ('tv', '115生活_安卓电视端'),
    ('qandroid', '115管理_安卓端'),
    ('web', '115生活_网页端（易触发风控，不推荐）'),
]
DEFAULT_APP = 'alipaymini'

# 扫码确认后，自动依次用这些设备身份尝试换取 Cookie，第一个成功即采用。
# 用户不必再反复换设备重扫 —— 一次扫码就把候选设备都覆盖掉。
FALLBACK_APPS = ['alipaymini', 'wechatmini', 'android', '115android',
                 'ios', 'ipad', 'os_windows', 'web']


class StorageError(Exception):
    """存储层业务异常（消息可直接展示给用户）"""


# --------------------------------------------------------------------------
# 扫码登录会话管理（进程内存，重启失效，符合预期）
# --------------------------------------------------------------------------
class QrSession:
    def __init__(self):
        self.uid = ''
        self.token = {}
        self.app = DEFAULT_APP
        self.created_at = 0
        self.status = 0          # 0 等待扫码 / 1 已扫码 / 2 已确认 / -1 过期 / -2 取消
        self.message = '等待扫码'
        self.cookies = ''
        self.done = False
        self.error = ''
        self.debug = []          # 诊断日志：每一步的原始响应（排查「参数错误」用）
        # 用哨兵值而非 None：等待扫码时 115 返回 data={}，raw_status 也是 None，
        # 与初值相同会导致第 3 步**完全不写诊断日志** —— 这正是用户反馈
        # 「诊断里只有 2 条记录、看不到错误」的原因。
        self._last_status_raw = '__init__'
        self.tried_apps = []     # 第 4 步换取凭据时试过的设备及结果
        self.app_used = ''       # 最终成功换取凭据所用的设备
        self.retry_count = 0     # 「还没确认」导致的退回重试次数（防死循环）

    def dbg(self, step, detail):
        if len(self.debug) < 80:
            self.debug.append({
                't': time.strftime('%H:%M:%S'),
                'step': step,
                'detail': detail if isinstance(detail, str) else json.dumps(
                    detail, ensure_ascii=False)[:600],
            })


_qr_sessions = {}
_qr_lock = threading.Lock()


def _tok_msg(resp):
    """把 115 的响应压成一句人话（含 errno，便于排查）"""
    if not isinstance(resp, dict):
        return '响应异常'
    msg = resp.get('message') or resp.get('error') or ''
    errno = resp.get('errno') or resp.get('code')
    if msg and errno:
        return '%s（errno=%s）' % (msg, errno)
    return msg or ('state=%s' % resp.get('state'))


def qr_new_session(app=DEFAULT_APP):
    """新建扫码会话，返回 (session_id, QrSession)

    关键：token / 二维码 / 换 cookie 这三步**必须使用同一个 app**。
    115 会把 app 当作会话的一部分校验，混用会返回「参数错误」之类的失败。
    （p115client 的 login_qrcode_token 默认 app='web'，如果扫完再用别的 app
      去换 cookie，就会 app 不匹配。）
    """
    app = (app or DEFAULT_APP).strip() or DEFAULT_APP
    sess = QrSession()
    sess.app = app
    try:
        resp = P115Client.login_qrcode_token(app=app)
    except Exception as e:  # noqa: BLE001
        raise StorageError('获取二维码失败：%s' % e)
    sess.dbg('1.取 token(app=%s)' % app, resp)
    if not resp or resp.get('state') != 1:
        raise StorageError('获取二维码失败：%s' % _tok_msg(resp))
    data = dict(resp.get('data') or {})
    sid = data.get('uid') or ''
    if not sid:
        raise StorageError('获取二维码失败：接口未返回 uid')
    sess.uid = sid
    sess.token = {k: data.get(k) for k in ('uid', 'time', 'sign')}
    sess.created_at = time.time()
    try:
        sess.qrcode_png = P115Client.login_qrcode(sid, app=app)
        sess.dbg('2.取二维码图(app=%s)' % app, 'PNG %d 字节' % len(sess.qrcode_png))
    except Exception as e:  # noqa: BLE001
        sess.dbg('2.取二维码图', '失败：%s' % e)
        raise StorageError('生成二维码图片失败：%s' % e)
    with _qr_lock:
        _qr_sessions[sid] = sess
        # 清理超过 15 分钟的旧会话
        for k in [k for k, v in _qr_sessions.items() if time.time() - v.created_at > 900]:
            _qr_sessions.pop(k, None)
    return sid, sess


def _obtain_cookie(sess):
    """依次用多个设备身份换取 Cookie，返回 (cookie, 尝试明细列表)

    为什么要多试：115 对不同设备命名空间的接受度不一样，同一个二维码
    用某个设备换取可能报「参数错误」，换一个就成功。与其让用户反复
    换设备重扫，不如一次扫码把候选设备都试一遍，并留下完整明细。

    提前退出的情况：返回「老乡验证失败」/「IP登录异常」说明其实还没确认
    或已被风控，继续试别的设备没有意义，直接停手以节省请求。
    """
    order = [sess.app] + [a for a in FALLBACK_APPS if a != sess.app]
    tried = []
    for app in order:
        try:
            result = P115Client.login_qrcode_scan_result(sess.uid, app=app)
        except Exception as e:  # noqa: BLE001
            tried.append({'app': app, 'ok': False,
                          'detail': '%s: %s' % (type(e).__name__, str(e)[:120])})
            continue

        data = (result or {}).get('data') or {}
        cookie = data.get('cookie') or ''
        if cookie:
            tried.append({'app': app, 'ok': True,
                          'detail': '成功（cookie %d 字符）' % len(cookie)})
            sess.dbg('4.换取凭据：成功（app=%s，共试 %d 个）' % (app, len(tried)),
                     {'明细': tried})
            return cookie, tried

        detail = _tok_msg(result)
        tried.append({'app': app, 'ok': False, 'detail': detail})
        if any(k in detail for k in ('老乡', 'IP登录异常', 'IP 登录异常')):
            break

    sess.dbg('4.换取凭据：全部失败（起始 app=%s）' % sess.app, {'明细': tried})
    return '', tried


def qr_poll(sid):
    """轮询扫码状态。返回 QrSession（含 cookies / error / message / debug）"""
    with _qr_lock:
        sess = _qr_sessions.get(sid)
    if not sess:
        raise StorageError('二维码会话不存在或已过期，请重新获取')
    if sess.done:
        return sess
    if time.time() - sess.created_at > 900:
        sess.status = -1
        sess.message = '二维码已过期'
        sess.done = True
        return sess

    # ---- 第 3 步：查扫码状态 ----
    try:
        resp = P115Client.login_qrcode_scan_status(dict(sess.token))
    except Exception as e:  # noqa: BLE001
        sess.message = '查询状态失败：%s' % e
        sess.dbg('3.查状态', '异常：%s' % e)
        return sess

    # 外层 state 是"接口调用是否成功"，扫码进度在 data.status
    # 115 在「等待扫码」阶段可能返回 data:{}，此时按 0 处理
    raw_status = ((resp or {}).get('data') or {}).get('status')
    status = 0 if raw_status is None else raw_status
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0

    # 状态变化时记一次；**接口报错时也必须记** —— 否则错误会被"等待扫码"吞掉，
    # 用户看到的诊断里就只有取 token / 取二维码两条，无从排查。
    api_err = isinstance(resp, dict) and resp.get('state') != 1
    if raw_status != sess._last_status_raw or api_err:
        sess.dbg('3.查状态(app=%s, uid=%s…)' % (sess.app, (sess.uid or '')[:8]),
                 {'解析出的 status': raw_status, '原始响应': resp})
        sess._last_status_raw = raw_status

    # 接口层面报错（state != 1）时**必须让用户看见**，不能默默当"还在等待"
    if isinstance(resp, dict) and resp.get('state') != 1 and not sess.cookies:
        sess.error = _tok_msg(resp)
        sess.message = '查询扫码状态被拒：%s' % sess.error

    sess.status = status
    if status == 0:
        if not sess.error:
            sess.message = '等待扫码'
    elif status == 1:
        sess.message = '已扫码，请在手机上点「确认登录」'
    elif status == 2:
        sess.message = '已确认，正在获取登录凭据…'

        # ---- 第 4 步：换取 cookie（一次扫码，自动轮试多个设备身份）----
        cookie, tried = _obtain_cookie(sess)
        sess.tried_apps = tried

        if cookie:
            sess.app_used = next((t['app'] for t in tried if t['ok']), sess.app)
            sess.cookies = cookie
            sess.message = '登录成功（设备：%s）' % sess.app_used
            sess.done = True
        else:
            first = tried[0]['detail'] if tried else '未知错误'
            sess.error = '；'.join(
                '%s → %s' % (t['app'], t['detail']) for t in tried)[:400]
            # 「老乡验证失败 / IP异常」通常意味着手机上还没点确认，或已被风控。
            # 退回等待态让用户继续操作，但限制重试轮次，避免一直空转请求。
            if any(k in first for k in ('老乡', '验证失败', 'IP')) and sess.retry_count < 3:
                sess.retry_count += 1
                sess.status = 1
                sess._last_status_raw = '__reset__'
                sess.message = ('手机上可能还没点「确认登录」（115 返回：%s），'
                                '请在 115 App 里确认后稍等几秒' % first)
            else:
                hint = ''
                if '参数' in first:
                    hint = '｜已自动试过 %d 个设备都不行，点「复制诊断」发出来定位' % len(tried)
                elif 'IP' in first or '老乡' in first:
                    hint = '｜115 风控拦截了本机 IP：换网络/挂代理重试，或改用「手动粘贴 Cookie」'
                sess.message = '获取登录凭据失败：%s%s' % (first, hint)
                sess.done = True
    elif status == -1:
        sess.message = '二维码已过期，请重新获取'
        sess.done = True
    elif status == -2:
        sess.message = '已取消登录'
        sess.done = True
    else:
        sess.message = '未知状态 %s' % status
    return sess


def qr_cancel(sid):
    with _qr_lock:
        _qr_sessions.pop(sid, None)


def _time_skew_check():
    """对比本机时间与 115 服务器时间（HTTP Date 头），返回 (是否正常, 说明)

    115 的扫码凭证（uid/time/sign）带签名校验，本机时间偏差过大时会
    出现各种莫名报错。这是排查「参数错误」时最容易被忽略的一环。
    """
    try:
        import email.utils
        import urllib.request
        req = urllib.request.Request(
            'https://qrcodeapi.115.com/get/status/',
            headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            date_hdr = r.headers.get('Date')
        if not date_hdr:
            return True, '服务器未返回 Date 头，跳过'
        server_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
        skew = abs(time.time() - server_ts)
        if skew < 300:
            return True, '与 115 服务器偏差 %.0f 秒，正常' % skew
        return False, ('与 115 服务器偏差 %.0f 秒 ⚠️ 超过 5 分钟会导致登录校验失败，'
                       '请在飞牛「系统设置 → 时间」校准或配置 NTP' % skew)
    except Exception as e:  # noqa: BLE001
        return True, '检查失败（不影响登录）：%s' % str(e)[:80]


def qr_selftest(app=DEFAULT_APP):
    """自检扫码登录各环节，返回每一步的原始结果（用于排查"参数错误"）

    不需要真的扫码，只验证三个接口是否都通、app 是否一致可用。
    """
    app = (app or DEFAULT_APP).strip() or DEFAULT_APP
    steps = []

    def add(name, ok, detail):
        steps.append({'step': name, 'ok': bool(ok), 'detail': str(detail)[:300]})

    # 1) 取 token
    try:
        tok = P115Client.login_qrcode_token(app=app)
        ok = bool(tok) and tok.get('state') == 1
        add('1. 获取二维码 token（app=%s）' % app, ok, _tok_msg(tok) if not ok else
            'uid=%s' % ((tok.get('data') or {}).get('uid', ''))[:16] + '…')
        uid = (tok.get('data') or {}).get('uid') if ok else None
    except Exception as e:  # noqa: BLE001
        add('1. 获取二维码 token（app=%s）' % app, False, e)
        uid = None

    # 2) 取二维码图片（必须用同一个 app）
    if uid:
        try:
            png = P115Client.login_qrcode(uid, app=app)
            add('2. 生成二维码图片', len(png) > 100, '%d 字节' % len(png))
        except Exception as e:  # noqa: BLE001
            add('2. 生成二维码图片', False, e)

    # 3) 查状态（未扫码时应返回 data:{} 或 status=0）
    if uid:
        try:
            tok2 = P115Client.login_qrcode_token(app=app)
            d2 = dict(tok2.get('data') or {})
            st = P115Client.login_qrcode_scan_status(
                {k: d2.get(k) for k in ('uid', 'time', 'sign')})
            ok = bool(st) and st.get('state') == 1
            status = ((st.get('data') or {}).get('status'))
            add('3. 轮询扫码状态', ok, 'state=%s data.status=%s（未扫码时为空或 0 属正常）'
                % (st.get('state'), status))
        except Exception as e:  # noqa: BLE001
            add('3. 轮询扫码状态', False, e)

    # 4) 时间同步（偏差过大会导致扫码凭证签名校验失败）
    add('4. 时间同步检查', *_time_skew_check())

    return {'app': app, 'steps': steps,
            'all_ok': all(s['ok'] for s in steps)}


# --------------------------------------------------------------------------
# 结果解析小工具
# --------------------------------------------------------------------------
def _pick_id(data):
    """从各种 115 响应里挖出目录 id"""
    if not isinstance(data, dict):
        return None
    for key in ('cid', 'id', 'file_id', 'fid', 'category_id'):
        v = data.get(key)
        if v is not None and str(v).isdigit():
            return int(v)
    for key in ('data', 'info', 'category'):
        v = data.get(key)
        if isinstance(v, dict):
            got = _pick_id(v)
            if got is not None:
                return got
    return None


def _friendly_error(e):
    """把底层异常翻译成人话"""
    text = str(e)
    low = text.lower()
    if isinstance(e, P115OSError) or 'state' in low:
        pass
    if '登录' in text or 'cookie' in low or '405' in text or 'auth' in low:
        return '登录状态失效，请到「账号管理」重新扫码登录（%s）' % text
    if '空间' in text or 'space' in low:
        return '网盘空间不足（%s）' % text
    if 'repeated' in low or '已接收' in text or '重复' in text:
        return '文件已接收过，无需重复接收'
    if 'timed out' in low or 'timeout' in low:
        return '请求超时，请检查网络或稍后重试（%s）' % text
    if 'Connection' in text or 'Max retries' in text:
        return '无法连接 115 服务器，请检查容器网络（%s）' % text
    return text


def _p115_call(fn, payload=None, **kw):
    """统一调用 + 响应校验 + 异常翻译"""
    try:
        if payload is None:
            resp = fn(**kw)
        elif kw:
            resp = fn(payload, **kw)
        else:
            resp = fn(payload)
        return check_response(resp)
    except P115OSError as e:
        raise StorageError(_friendly_error(e)) from e
    except StorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise StorageError(_friendly_error(e)) from e


# --------------------------------------------------------------------------
# 主存储类
# --------------------------------------------------------------------------
class Storage115:
    def __init__(self):
        self.config = {}
        self._clients = {}
        self._lock = threading.RLock()
        self._load_config()

    # ---------------- 配置 ----------------

    def _load_config(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if not os.path.exists(CONFIG_PATH):
            self._create_config_from_template()
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        except (OSError, ValueError) as e:
            raise StorageError('读取配置文件失败：%s' % e)
        self.config.setdefault('p115', {})
        self.config['p115'].setdefault('users', {})
        self.config['p115'].setdefault('current_user', None)
        self.config['p115'].setdefault('tasks', [])
        return self.config

    def _create_config_from_template(self):
        if os.path.exists(TEMPLATE_PATH):
            with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {'auth': {'username': 'admin', 'password': 'zxcvbnm', 'session_timeout': 3600},
                    'p115': {'users': {}, 'current_user': None, 'tasks': []}}
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _save_config(self):
        with self._lock:
            tmp = CONFIG_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            os.replace(tmp, CONFIG_PATH)

    def reload(self):
        with self._lock:
            self._clients.clear()
            self._load_config()

    # ---------------- 客户端 ----------------

    def _client_for(self, username):
        username = username or self.config['p115'].get('current_user')
        if not username:
            raise StorageError('还没有添加任何 115 账号，请先在「账号管理」扫码登录')
        user = self.config['p115']['users'].get(username)
        if not user:
            raise StorageError('账号「%s」不存在' % username)
        cookies = (user or {}).get('cookies') or ''
        if not cookies:
            raise StorageError('账号「%s」没有 Cookie，请重新登录' % username)
        with self._lock:
            cached = self._clients.get(username)
            if cached and cached[0] == cookies:
                return cached[1]
            try:
                client = P115Client(cookies)
            except Exception as e:  # noqa: BLE001
                raise StorageError('初始化 115 客户端失败：%s' % _friendly_error(e))
            self._clients[username] = (cookies, client)
            return client

    def current_client(self):
        return self._client_for(None)

    def client_of(self, username):
        return self._client_for(username)

    # ---------------- 账号 ----------------

    def list_users(self):
        users = self.config['p115'].get('users') or {}
        out = []
        for name, info in users.items():
            out.append({
                'username': name,
                'uid': (info or {}).get('uid') or '',
                'app': (info or {}).get('app') or '',
                'added_at': (info or {}).get('added_at') or '',
                'last_check': (info or {}).get('last_check') or '',
                'last_check_ok': (info or {}).get('last_check_ok'),
                'remark': (info or {}).get('remark') or '',
                'current': name == self.config['p115'].get('current_user'),
            })
        return out

    @staticmethod
    def _uid_from_cookies(cookies):
        m = re.search(r'(?:^|;\s*)UID=([^;]+)', cookies or '')
        return m.group(1) if m else ''

    def add_user(self, username, cookies, app=DEFAULT_APP, remark='', make_current=True):
        cookies = (cookies or '').strip()
        if not cookies:
            raise StorageError('Cookie 不能为空')
        missing = [k for k in ('UID', 'CID', 'SEID', 'KID') if k not in cookies]
        if missing:
            raise StorageError('Cookie 缺少字段：%s（需要 UID/CID/SEID/KID 四项齐全）' % '、'.join(missing))
        username = (username or '').strip() or self._uid_from_cookies(cookies) or ('账号%s' % (len(self.list_users()) + 1))
        users = self.config['p115']['users']
        users[username] = {
            'cookies': cookies,
            'uid': self._uid_from_cookies(cookies),
            'app': app or DEFAULT_APP,
            'remark': remark or '',
            'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        if make_current or not self.config['p115'].get('current_user'):
            self.config['p115']['current_user'] = username
        self._save_config()
        with self._lock:
            self._clients.pop(username, None)
        return username

    def update_user(self, username, cookies=None, remark=None):
        users = self.config['p115']['users']
        if username not in users:
            raise StorageError('账号「%s」不存在' % username)
        if cookies:
            cookies = cookies.strip()
            missing = [k for k in ('UID', 'CID', 'SEID', 'KID') if k not in cookies]
            if missing:
                raise StorageError('Cookie 缺少字段：%s' % '、'.join(missing))
            users[username]['cookies'] = cookies
            users[username]['uid'] = self._uid_from_cookies(cookies)
        if remark is not None:
            users[username]['remark'] = remark
        self._save_config()
        with self._lock:
            self._clients.pop(username, None)
        return True

    def remove_user(self, username):
        users = self.config['p115']['users']
        if username not in users:
            raise StorageError('账号「%s」不存在' % username)
        users.pop(username)
        if self.config['p115'].get('current_user') == username:
            self.config['p115']['current_user'] = next(iter(users), None)
        self._save_config()
        with self._lock:
            self._clients.pop(username, None)
        return True

    def switch_user(self, username):
        if username not in (self.config['p115'].get('users') or {}):
            raise StorageError('账号「%s」不存在' % username)
        self.config['p115']['current_user'] = username
        self._save_config()
        return True

    def check_user(self, username=None):
        """校验账号可用性，返回用户信息摘要"""
        username = username or self.config['p115'].get('current_user')
        client = self._client_for(username)
        info = self._user_summary(client)
        users = self.config['p115']['users']
        if username in users:
            users[username]['last_check'] = time.strftime('%Y-%m-%d %H:%M:%S')
            users[username]['last_check_ok'] = True
            if info.get('uid'):
                users[username]['uid'] = info['uid']
            self._save_config()
        info['username'] = username
        return info

    def _user_summary(self, client):
        out = {'nickname': '', 'uid': '', 'vip': '', 'space_used': 0,
               'space_total': 0, 'space_percent': 0, 'space_used_text': '', 'space_total_text': ''}
        try:
            my = client.user_my()
            data = (my or {}).get('data') or {}
            out['nickname'] = data.get('uname') or data.get('user_name') or ''
            out['uid'] = str(data.get('user_id') or data.get('uid') or '')
        except Exception:  # noqa: BLE001
            pass
        try:
            sp = client.user_space_info()
            data = (sp or {}).get('data') or {}
            # 不同 app 返回字段略有差异，做兼容
            total = data.get('all_total') or data.get('total') or data.get('all_size')
            used = data.get('all_use') or data.get('use') or data.get('use_size')
            size = data.get('all_size') or data.get('size') or data.get('all_remain')
            if size is not None:
                total = int(size)
            total = int(total or 0)
            used = int(used or 0)
            if total:
                out['space_used'] = used
                out['space_total'] = total
                out['space_percent'] = round(used * 100.0 / total, 2)
                out['space_used_text'] = human_size(used)
                out['space_total_text'] = human_size(total)
        except Exception as e:  # noqa: BLE001
            logger.warning('获取空间信息失败: %s' % e)
        return out

    def space_info(self, username=None):
        return self._user_summary(self._client_for(username or None))

    # ---------------- 路径 / 目录 ----------------

    @staticmethod
    def normalize_path(path):
        path = (path or '/').strip().replace('\\', '/')
        if not path.startswith('/'):
            path = '/' + path
        path = re.sub(r'/+', '/', path)
        if path != '/' and path.endswith('/'):
            path = path.rstrip('/')
        return path or '/'

    def dir_id(self, path, client=None):
        """把 115 路径解析成目录 id（目录不存在则抛错）"""
        path = self.normalize_path(path)
        if path == '/':
            return 0
        client = client or self.current_client()
        resp = _p115_call(client.fs_dir_getid, {'path': path})
        cid = _pick_id(resp)
        if cid is None:
            raise StorageError('目录「%s」不存在' % path)
        return cid

    def ensure_dir(self, path, client=None):
        """确保目录存在（自动创建中间层级），返回目录 id"""
        path = self.normalize_path(path)
        if path == '/':
            return 0
        client = client or self.current_client()
        try:
            return self.dir_id(path, client)
        except StorageError:
            pass
        resp = _p115_call(client.fs_makedirs, {'path': path})
        cid = _pick_id(resp)
        if cid is None:
            cid = self.dir_id(path, client)
        return cid

    def list_dir(self, cid=0, offset=0, limit=100, client=None, only_dir=False):
        """罗列目录内容，返回 {'cid', 'total', 'items': [...]}"""
        client = client or self.current_client()
        payload = {
            'cid': int(cid or 0),
            'offset': int(offset or 0),
            'limit': int(limit or 100),
            'show_dir': 1,
            'natsort': 0,
            'record_open_time': 0,
            'fc_mix': 0,
        }
        if only_dir:
            payload['type'] = 0
        resp = client.fs_files(payload)
        # fs_files 在「空目录」等情况下 state 可能为 0，这里做宽容处理
        if isinstance(resp, dict) and resp.get('state') in (0, False) and not resp.get('data'):
            raise StorageError(_friendly_error(resp.get('message') or resp))
        resp = resp or {}
        items = []
        for raw in (resp.get('data') or []):
            try:
                items.append(normalize_attr_simple(raw))
            except Exception:  # noqa: BLE001
                items.append({'is_dir': False, 'id': raw.get('fid'), 'name': raw.get('n') or '',
                              'size': int(raw.get('s') or 0), 'sha1': raw.get('sha') or '',
                              'pickcode': raw.get('pc') or '', 'mtime': 0, 'ctime': 0, 'type': 99})
        items.sort(key=lambda x: (not x.get('is_dir'), (x.get('name') or '').lower()))
        return {'cid': int(cid or 0), 'total': int(resp.get('count') or len(items)), 'items': items}

    def list_children_index(self, cid, client=None, max_items=20000):
        """把目录下所有直接子项建成索引：{小写名: item} 与 {sha1: item}，用于去重"""
        client = client or self.current_client()
        by_name, by_sha1 = {}, {}
        offset, limit = 0, 500
        while offset < max_items:
            page = self.list_dir(cid, offset=offset, limit=limit, client=client)
            items = page['items']
            if not items:
                break
            for it in items:
                name = (it.get('name') or '').strip().lower()
                if name:
                    by_name.setdefault(name, it)
                sha = (it.get('sha1') or '').strip().upper()
                if sha:
                    by_sha1.setdefault(sha, it)
            if len(items) < limit:
                break
            offset += limit
        return by_name, by_sha1

    def path_tree(self, cid=0, client=None):
        """路径选择器用：返回某层的子目录列表"""
        page = self.list_dir(cid, limit=1000, client=client, only_dir=True)
        return [{'id': i['id'], 'name': i['name']} for i in page['items'] if i.get('is_dir')]

    # ---------------- 任务 ----------------

    @staticmethod
    def normalize_task(task, order=None):
        task = dict(task or {})
        task.setdefault('name', '')
        task.setdefault('url', '')
        task.setdefault('pwd', '')
        task.setdefault('save_dir', '/')
        task.setdefault('compare_path', '')
        task.setdefault('regex_pattern', '')
        task.setdefault('regex_replace', '')
        task.setdefault('cron', '')
        task.setdefault('category', '')
        task.setdefault('enabled', True)
        task.setdefault('include_subdirs', True)
        task.setdefault('exclude_files', [])
        task.setdefault('transfer_file_ids', [])
        task.setdefault('dedupe_mode', 'name')      # name | name_size | sha1
        task.setdefault('task_uid', '')
        task.setdefault('status', 'idle')
        task.setdefault('last_run', '')
        task.setdefault('last_message', '')
        task.setdefault('last_new_count', 0)
        if not task['task_uid']:
            # 用 url 派生一个稳定 id：保证多次 normalize 结果一致，不会「每次生成新 id」
            seed = task.get('url') or ('order-%s' % task.get('order', 0))
            task['task_uid'] = 't' + hashlib.md5(seed.encode('utf-8')).hexdigest()[:10]
        if order is not None:
            task['order'] = int(order)
        task.setdefault('order', 0)
        return task

    def list_tasks(self):
        tasks = self.config['p115'].get('tasks') or []
        normalized = [self.normalize_task(t, i + 1) for i, t in enumerate(tasks)]
        return normalized

    def get_max_order(self):
        return len(self.config['p115'].get('tasks') or [])

    def get_task_by_uid(self, task_uid):
        for t in self.list_tasks():
            if t.get('task_uid') == task_uid:
                return t
        return None

    def resolve_task(self, task_uid=None, order=None, url=None):
        tasks = self.list_tasks()
        if task_uid:
            for t in tasks:
                if t.get('task_uid') == task_uid:
                    return t
        if order is not None:
            for t in tasks:
                if int(t.get('order', 0)) == int(order):
                    return t
        if url:
            for t in tasks:
                if t.get('url') == url:
                    return t
        return None

    def add_task(self, url, save_dir, pwd=None, name=None, cron=None, category=None,
                 regex_pattern=None, regex_replace=None, **extra):
        url = (url or '').strip()
        if not url:
            raise StorageError('分享链接不能为空')
        if not save_dir:
            raise StorageError('保存路径不能为空')
        share_code, receive_code = self.parse_share(url, pwd)
        tasks = self.config['p115'].setdefault('tasks', [])
        task = self.normalize_task({
            'url': url,
            'save_dir': self.normalize_path(save_dir),
            'pwd': pwd or receive_code or '',
            'name': name or '',
            'cron': cron or '',
            'category': category or '',
            'regex_pattern': regex_pattern or '',
            'regex_replace': regex_replace or '',
            **extra,
        }, order=len(tasks) + 1)
        # 没填名字就自动抓取分享的标题
        if not task['name']:
            try:
                task['name'] = self.share_title(share_code, task['pwd']) or ('任务%s' % task['order'])
            except Exception:  # noqa: BLE001
                task['name'] = '任务%s' % task['order']
        tasks.append(task)
        self._save_config()
        return task

    def update_task(self, task_uid, data):
        tasks = self.config['p115'].setdefault('tasks', [])
        for i, t in enumerate(tasks):
            cur = self.normalize_task(t, i + 1)
            if cur.get('task_uid') == task_uid:
                merged = dict(cur)
                for k, v in (data or {}).items():
                    if k in ('task_uid', 'order'):
                        continue
                    merged[k] = v
                if 'save_dir' in (data or {}):
                    merged['save_dir'] = self.normalize_path(merged['save_dir'])
                if 'compare_path' in (data or {}):
                    merged['compare_path'] = self.normalize_path(merged['compare_path']) if merged['compare_path'] else ''
                tasks[i] = merged
                self._save_config()
                return self.normalize_task(merged, i + 1)
        raise StorageError('任务不存在')

    def remove_task(self, task_uid):
        tasks = self.config['p115'].setdefault('tasks', [])
        new_tasks = [t for t in tasks if self.normalize_task(t).get('task_uid') != task_uid]
        if len(new_tasks) == len(tasks):
            raise StorageError('任务不存在')
        self.config['p115']['tasks'] = new_tasks
        self._save_config()
        return True

    def remove_tasks(self, task_uids):
        wanted = set(task_uids or [])
        tasks = self.config['p115'].setdefault('tasks', [])
        self.config['p115']['tasks'] = [t for t in tasks if self.normalize_task(t).get('task_uid') not in wanted]
        self._save_config()
        return True

    def reorder_task(self, task_uid, new_order):
        tasks = self.list_tasks()
        idx = next((i for i, t in enumerate(tasks) if t['task_uid'] == task_uid), None)
        if idx is None:
            raise StorageError('任务不存在')
        item = tasks.pop(idx)
        new_order = max(1, int(new_order))
        tasks.insert(min(new_order - 1, len(tasks)), item)
        self.config['p115']['tasks'] = tasks
        self._save_config()
        return True

    def update_task_status(self, task_uid, status, message=None, new_count=None):
        tasks = self.config['p115'].setdefault('tasks', [])
        for i, t in enumerate(tasks):
            if self.normalize_task(t).get('task_uid') == task_uid:
                tasks[i] = {**t, 'status': status, 'last_run': time.strftime('%Y-%m-%d %H:%M:%S')}
                if message is not None:
                    tasks[i]['last_message'] = message
                if new_count is not None:
                    tasks[i]['last_new_count'] = new_count
                self._save_config()
                return True
        return False

    # ---------------- 分享 ----------------

    @staticmethod
    def parse_share(url, pwd=None):
        """解析分享链接，返回 (share_code, receive_code)"""
        url = (url or '').strip()
        if not url:
            raise StorageError('分享链接不能为空')
        share_code, receive_code = '', ''
        try:
            payload = p115tool.share_extract_payload(url)
            share_code = (payload or {}).get('share_code') or ''
            receive_code = (payload or {}).get('receive_code') or ''
        except Exception:  # noqa: BLE001
            pass
        if not share_code:
            m = re.search(r'/(?:s|web/share)/([A-Za-z0-9_\-]+)', url)
            if m:
                share_code = m.group(1)
        if not share_code and re.fullmatch(r'[A-Za-z0-9_\-]{6,}', url):
            share_code = url            # 用户直接粘的分享码
        if not receive_code:
            m = re.search(r'(?:password|pwd|code)=([A-Za-z0-9]{1,8})', url)
            if m:
                receive_code = m.group(1)
        if not receive_code and pwd:
            receive_code = str(pwd).strip()
        if not share_code:
            raise StorageError('无法从链接里识别分享码，请检查链接：%s' % url)
        return share_code, receive_code

    def _share_fs(self, url, pwd=None, client=None):
        client = client or self.current_client()
        share_code, receive_code = self.parse_share(url, pwd)
        return P115ShareFileSystem(client, share_code, receive_code or None), share_code, receive_code

    def share_title(self, share_code, receive_code=''):
        client = self.current_client()
        fs = P115ShareFileSystem(client, share_code, receive_code or None)
        info = fs.share_info or {}
        for key in ('share_title', 'title', 'share_name'):
            if info.get(key):
                return str(info[key])
        return ''

    def share_browse(self, url, pwd=None, cid=0):
        """浏览分享目录（用于「选择转存文件夹」），返回 {'title', 'cid', 'items'}"""
        fs, share_code, receive_code = self._share_fs(url, pwd)
        items = []
        try:
            raw_items = list(fs.iterdir(int(cid or 0)))
        except StorageError:
            raise
        except Exception as e:  # noqa: BLE001
            raise StorageError(_friendly_error(e))
        for it in raw_items:
            items.append({
                'id': it.get('id'),
                'name': it.get('name') or '',
                'is_dir': bool(it.get('is_dir')),
                'size': int(it.get('size') or 0),
                'sha1': it.get('sha1') or '',
            })
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        title = ''
        try:
            info = fs.share_info or {}
            title = info.get('share_title') or info.get('title') or ''
        except Exception:  # noqa: BLE001
            pass
        return {'title': title, 'cid': int(cid or 0), 'items': items,
                'share_code': share_code, 'receive_code': receive_code}

    @staticmethod
    def apply_regex(file_path, task):
        """按正则改写路径（返回 None 表示被过滤掉）"""
        pattern = (task or {}).get('regex_pattern') or ''
        if not pattern:
            return file_path
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise StorageError('正则表达式无效：%s' % e)
        name = file_path.rsplit('/', 1)[-1]
        if not rx.search(name):
            return None
        replace = (task or {}).get('regex_replace') or ''
        if replace:
            try:
                return rx.sub(replace, file_path)
            except re.error as e:
                raise StorageError('正则替换失败：%s' % e)
        return file_path

    def transfer_share(self, task, progress=None):
        """执行一次转存。

        返回 dict：
            success, message, file_count, new_items[list], skipped[list],
            save_dir, target_cid, log[list[tuple[level, msg]]]
        """
        log = []

        def emit(msg, level='INFO'):
            log.append((level, msg))
            logger.log(level if level in ('DEBUG', 'INFO', 'WARNING', 'ERROR') else 'INFO', msg)
            if progress:
                try:
                    progress(msg, level)
                except Exception:  # noqa: BLE001
                    pass

        task = self.normalize_task(task)
        result = {'success': False, 'message': '', 'file_count': 0, 'new_items': [],
                  'skipped': [], 'save_dir': task.get('save_dir') or '/', 'target_cid': 0, 'log': log}
        client = self.current_client()

        emit('开始处理任务「%s」' % (task.get('name') or task.get('url')))
        share_code, receive_code = self.parse_share(task['url'], task.get('pwd'))
        emit('分享码：%s，提取码：%s' % (share_code, receive_code or '（无）'))

        # 1) 目标目录
        target_cid = self.ensure_dir(task['save_dir'], client)
        result['target_cid'] = target_cid
        emit('保存目录 %s → id=%s' % (task['save_dir'], target_cid))

        # 2) 分享内待转存项
        fs = P115ShareFileSystem(client, share_code, receive_code or None)
        selected = list(task.get('transfer_file_ids') or [])
        try:
            root_items = list(fs.iterdir(0))
        except Exception as e:  # noqa: BLE001
            raise StorageError('无法读取分享内容（链接可能已失效）：%s' % _friendly_error(e))
        if not root_items:
            result['message'] = '分享内容为空'
            emit('分享内容为空，结束', 'WARNING')
            return result

        root_norm = []
        for it in root_items:
            root_norm.append({
                'id': it.get('id'),
                'name': it.get('name') or '',
                'is_dir': bool(it.get('is_dir')),
                'size': int(it.get('size') or 0),
                'sha1': (it.get('sha1') or '').upper(),
            })
        emit('分享根目录共 %d 项' % len(root_norm))

        if selected:
            want = {str(x) for x in selected}
            candidates = [it for it in root_norm if str(it['id']) in want]
            emit('按「指定转存文件夹」筛选出 %d 项' % len(candidates))
            if not candidates:
                # 选中的可能是子层级的项，115 允许直接接收子项 id
                candidates = [{'id': int(s), 'name': '指定项#%s' % s, 'is_dir': False, 'size': 0, 'sha1': ''}
                              for s in selected]
        else:
            candidates = root_norm

        # 3) 正则过滤（按名字过滤顶层项）
        if task.get('regex_pattern'):
            kept = []
            for it in candidates:
                if it['is_dir']:
                    kept.append(it)
                    continue
                if self.apply_regex(it['name'], task) is not None:
                    kept.append(it)
                else:
                    emit('正则未命中，跳过：%s' % it['name'], 'DEBUG')
            candidates = kept

        # 4) 排除清单
        excludes = {str(x).lower() for x in (task.get('exclude_files') or [])}
        if excludes:
            before = len(candidates)
            candidates = [it for it in candidates if it['name'].lower() not in excludes]
            if before != len(candidates):
                emit('按排除清单跳过 %d 项' % (before - len(candidates)))

        # 5) 去重
        compare_dir = task.get('compare_path') or task['save_dir']
        try:
            compare_cid = self.ensure_dir(compare_dir, client)
        except StorageError:
            compare_cid = target_cid
        by_name, by_sha1 = self.list_children_index(compare_cid, client=client)
        emit('对比目录 %s 已索引 %d 个同名项' % (compare_dir, len(by_name)))

        mode = task.get('dedupe_mode') or 'name'
        to_receive, skipped = [], []
        for it in candidates:
            name_low = (it['name'] or '').strip().lower()
            hit = None
            if name_low and name_low in by_name:
                exist = by_name[name_low]
                if mode == 'name_size':
                    if int(exist.get('size') or 0) == int(it['size'] or 0):
                        hit = exist
                else:
                    hit = exist
            if hit is None and mode == 'sha1' and it.get('sha1') and it['sha1'] in by_sha1:
                hit = by_sha1[it['sha1']]
            if hit is not None:
                skipped.append(it['name'])
                emit('已存在，跳过：%s' % it['name'], 'DEBUG')
            else:
                to_receive.append(it)

        result['skipped'] = skipped
        if skipped:
            emit('去重跳过 %d 项' % len(skipped))
        if not to_receive:
            result['success'] = True
            result['message'] = '没有新文件需要转存（已跳过 %d 项）' % len(skipped)
            emit('没有新文件，结束')
            return result

        # 6) 接收
        emit('开始转存 %d 项到 %s' % (len(to_receive), task['save_dir']))
        ids = [it['id'] for it in to_receive]
        try:
            fs.receive(ids, to_pid=target_cid)
        except Exception as e:  # noqa: BLE001
            msg = _friendly_error(e)
            if '已接收' in msg or '重复' in msg:
                emit('115 提示文件已接收过，按成功处理', 'WARNING')
            else:
                raise StorageError(msg)
        emit('转存请求已提交')

        # 7) 复核：转存后重新列目录，确认新文件
        time.sleep(1.2)
        after_name, _ = self.list_children_index(target_cid, client=client)
        new_items = []
        for it in to_receive:
            key = (it['name'] or '').strip().lower()
            if key in after_name:
                new_items.append({'name': it['name'], 'is_dir': it['is_dir'],
                                  'size': it['size'], 'id': after_name[key].get('id')})
            else:
                new_items.append({'name': it['name'], 'is_dir': it['is_dir'], 'size': it['size'], 'id': None})

        result['new_items'] = new_items
        result['file_count'] = len(new_items)
        result['success'] = True
        result['message'] = '成功转存 %d 项' % len(new_items)
        emit(result['message'])
        return result

    # ---------------- 离线下载 ----------------

    def offline_add(self, urls, save_dir=None, client=None):
        """把一批链接（磁力/ed2k/HTTP）提交给 115 离线下载"""
        client = client or self.current_client()
        if isinstance(urls, str):
            urls = [u for u in re.split(r'[\r\n,\s]+', urls) if u.strip()]
        urls = [u.strip() for u in (urls or []) if u and u.strip()]
        if not urls:
            raise StorageError('请至少提供一个下载链接')
        payload = {}
        for i, u in enumerate(urls):
            payload['url[%d]' % i] = u
        if save_dir:
            try:
                payload['wp_path_id'] = self.ensure_dir(save_dir, client)
            except StorageError as e:
                raise StorageError('保存目录不可用：%s' % e)
        resp = client.clouddownload_task_add_urls(payload)
        if isinstance(resp, dict) and resp.get('state') in (0, False):
            raise StorageError(_friendly_error(resp.get('message') or resp))
        return {'submitted': len(urls), 'raw': resp}

    def offline_list(self, page=1, page_size=50, stat=None, client=None):
        client = client or self.current_client()
        payload = {'page': int(page or 1), 'page_size': int(page_size or 50)}
        if stat:
            payload['stat'] = int(stat)
        resp = client.clouddownload_task_list(payload)
        data = (resp or {}).get('data') if isinstance(resp, dict) else {}
        tasks = []
        if isinstance(data, dict):
            tasks = data.get('tasks') or []
        elif isinstance(data, list):
            tasks = data
        out = []
        for t in (tasks or []):
            out.append({
                'info_hash': t.get('info_hash') or t.get('hash') or '',
                'name': t.get('name') or t.get('file_name') or '',
                'url': t.get('url') or '',
                'size': int(t.get('size') or 0),
                'size_text': human_size(int(t.get('size') or 0)),
                'status': t.get('status'),
                'percent': t.get('percentDone') if t.get('percentDone') is not None else t.get('percent'),
                'add_time': t.get('add_time') or '',
                'file_id': t.get('file_id') or '',
                'raw_status': t.get('status'),
            })
        return {'page': int(page or 1), 'items': out,
                'count': (data or {}).get('count') if isinstance(data, dict) else len(out)}

    def offline_delete(self, hashes, client=None):
        client = client or self.current_client()
        hashes = [h for h in (hashes or []) if h]
        if not hashes:
            raise StorageError('请选择要删除的离线任务')
        payload = {}
        for i, h in enumerate(hashes):
            payload['hash[%d]' % i] = h
        payload['flag'] = 0
        resp = client.clouddownload_task_del(payload)
        if isinstance(resp, dict) and resp.get('state') in (0, False):
            raise StorageError(_friendly_error(resp.get('message') or resp))
        return True

    def offline_clear(self, flag=0, client=None):
        client = client or self.current_client()
        resp = client.clouddownload_task_clear({'flag': int(flag)})
        return resp

    def offline_restart(self, hashes, client=None):
        client = client or self.current_client()
        payload = {}
        for i, h in enumerate(hashes or []):
            payload['hash[%d]' % i] = h
        return client.clouddownload_task_restart(payload)

    # ---------------- 文件管理 ----------------

    def fs_mkdir(self, cid, name, client=None):
        client = client or self.current_client()
        if not (name or '').strip():
            raise StorageError('目录名不能为空')
        return _p115_call(client.fs_mkdir, {'cname': name.strip()}, pid=int(cid or 0))

    def fs_rename(self, fid, new_name, client=None):
        client = client or self.current_client()
        if not (new_name or '').strip():
            raise StorageError('新名称不能为空')
        return _p115_call(client.fs_rename, (int(fid), new_name.strip()))

    def fs_move(self, fids, target_cid, client=None):
        client = client or self.current_client()
        fids = [int(f) for f in (fids or [])]
        if not fids:
            raise StorageError('请选择要移动的项目')
        return _p115_call(client.fs_move, fids, pid=int(target_cid or 0))

    def fs_copy(self, fids, target_cid, client=None):
        client = client or self.current_client()
        fids = [int(f) for f in (fids or [])]
        if not fids:
            raise StorageError('请选择要复制的项目')
        return _p115_call(client.fs_copy, fids, pid=int(target_cid or 0))

    def fs_delete(self, fids, client=None):
        client = client or self.current_client()
        fids = [int(f) for f in (fids or [])]
        if not fids:
            raise StorageError('请选择要删除的项目')
        return _p115_call(client.fs_delete, fids)

    def fs_search(self, keyword, cid=0, limit=100, client=None):
        client = client or self.current_client()
        resp = _p115_call(client.fs_search, {'search_value': keyword, 'cid': int(cid or 0),
                                             'limit': int(limit), 'offset': 0})
        items = []
        for raw in (resp.get('data') or []):
            try:
                items.append(normalize_attr_simple(raw))
            except Exception:  # noqa: BLE001
                continue
        return items

    def share_create(self, fids, receive_code=None, days=None, client=None):
        """把自己的文件生成分享链接"""
        client = client or self.current_client()
        fids = [int(f) for f in (fids or [])]
        if not fids:
            raise StorageError('请选择要分享的项目')
        payload = {'file_ids': ','.join(str(f) for f in fids)}
        if receive_code:
            payload['receive_code'] = receive_code
        if days is not None:
            payload['share_duration'] = int(days)
        resp = _p115_call(client.share_send, payload)
        data = resp.get('data') or resp
        code = data.get('share_code') or data.get('code') or ''
        rcode = data.get('receive_code') or receive_code or ''
        url = ('https://115.com/s/%s' % code) if code else ''
        if url and rcode:
            url += '?password=%s' % rcode
        return {'share_url': url, 'share_code': code, 'receive_code': rcode, 'raw': resp}


def human_size(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return '0 B'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if abs(num) < 1024.0:
            return ('%.2f %s' % (num, unit)) if unit != 'B' else ('%d B' % num)
        num /= 1024.0
    return '%.2f EB' % num

# -*- coding: utf-8 -*-
"""应用内「检查更新」与「一键更新」

分两级能力，按运行环境自动降级：

1. **检查更新**（零依赖）
   查 GitHub Releases 的最新版，与当前 APP_VERSION 比对。
   容器只要通网就能用。

2. **一键更新**（需容器挂载 /var/run/docker.sock）
   在面板上点一下就完成「拉新镜像 → 重建容器」，不用去飞牛的 Docker
   界面手动操作。

   ⚠️ 关键技术难点：**容器无法自己重建自己** —— 一旦被 stop，进程就死了，
   后面的重建动作没人执行。所以这里用「**更新助手容器**」：当前容器通过
   Docker Engine API 启动一个一次性助手容器，由它在我们退出之后继续干活：

       助手: sleep 3 → cd <compose 项目目录> → docker compose pull
             → docker compose up -d --force-recreate → 自我删除

   之所以走 `docker compose`（而不是用 API 手拼容器参数），是因为
   compose 文件里有**权威配置** —— 端口、挂载、环境变量、网络模式全部
   以它为准，不会因为程序重建而丢失或走样。

   compose 项目目录直接从容器 label
   `com.docker.compose.project.working_dir` 读取，所以不管用户是在飞牛
   界面粘贴 YAML 还是 SSH 里手写，位置都能自动找到。

⚠️ **安全提示**：把 docker.sock 挂进容器，等于把宿主机 Docker 的控制权
   交给这个容器（等价 root）。因此本模块**只在检测到 socket 可用时才启用**
   一键更新，并且可以通过配置项 `allow_self_update` 关闭。
   不需要这个功能就**不要挂 socket** —— 检查更新依然可用。
"""
import http.client
import json
import os
import re
import socket
import threading
import time

from loguru import logger

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
GITHUB_REPO = 'xiaoming677/115savepro'
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET') or '/var/run/docker.sock'
# 助手容器镜像：需要自带 docker CLI 与 compose 插件
ASSISTANT_IMAGE = os.environ.get('UPDATE_ASSISTANT_IMAGE') or 'docker:cli'
ASSISTANT_NAME = '115savepro-updater'

_check_cache = {'at': 0, 'data': None}
_check_lock = threading.Lock()


# --------------------------------------------------------------------------
# Docker Engine API（走 unix socket，不依赖 docker CLI，也不额外装库）
# --------------------------------------------------------------------------
class _UnixHTTPConnection(http.client.HTTPConnection):
    """让 http.client 走 unix socket 的最小实现"""

    def __init__(self, socket_path, timeout=120):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _docker(method, path, body=None, timeout=120):
    """调 Docker Engine API。返回 (http_status, 解析后的对象/原始文本)"""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    headers = {'Host': 'docker'}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        return resp.status, json.loads(raw.decode('utf-8') or '{}')
    except Exception:  # noqa: BLE001
        return resp.status, raw.decode('utf-8', 'replace')


def socket_available():
    """检测 docker.sock 是否真的可读写（挂载了且权限正确）"""
    if not os.path.exists(DOCKER_SOCKET):
        return False, '未挂载 %s' % DOCKER_SOCKET
    try:
        st, obj = _docker('GET', '/version', timeout=10)
        if st == 200 and isinstance(obj, dict):
            return True, obj.get('Version') or 'ok'
        return False, '访问 Docker 失败：HTTP %s' % st
    except PermissionError:
        return False, '没有权限访问 %s（容器内非 root 或 socket 属主不符）' % DOCKER_SOCKET
    except Exception as e:  # noqa: BLE001
        return False, '%s: %s' % (type(e).__name__, str(e)[:120])


def _self_container_id():
    """拿到自身容器 ID。Docker 默认把容器短 ID 写进 HOSTNAME"""
    for key in ('SELF_CONTAINER_ID', 'HOSTNAME'):
        v = (os.environ.get(key) or '').strip()
        if v:
            return v
    try:
        with open('/etc/hostname', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ''


def self_info():
    """读取自身容器的关键信息（用于展示与构造助手容器）"""
    cid = _self_container_id()
    if not cid:
        raise RuntimeError('无法确定自身容器 ID')
    st, obj = _docker('GET', '/containers/%s/json' % cid, timeout=30)
    if st != 200 or not isinstance(obj, dict):
        raise RuntimeError('读取容器信息失败：HTTP %s %s' % (st, str(obj)[:120]))

    cfg = obj.get('Config') or {}
    labels = cfg.get('Labels') or {}
    host = obj.get('HostConfig') or {}

    # compose 项目目录：飞牛界面粘贴 YAML 的情况下也能靠这个 label 找到真实位置
    workdir = (labels.get('com.docker.compose.project.working_dir') or '').strip()
    project = (labels.get('com.docker.compose.project') or '').strip()

    # docker.sock 在宿主机上的真实路径（用户可能挂到别处）
    sock_host = ''
    for b in host.get('Binds') or []:
        parts = b.split(':')
        if len(parts) >= 2 and 'docker.sock' in parts[0]:
            sock_host = parts[0]
            break

    return {
        'id': obj.get('Id') or cid,
        'name': (obj.get('Name') or '').lstrip('/'),
        'image': cfg.get('Image') or '',
        'workdir': workdir,
        'project': project,
        'sock_host_path': sock_host or '/var/run/docker.sock',
        'started_at': (obj.get('State') or {}).get('StartedAt') or '',
    }


# --------------------------------------------------------------------------
# 版本检查
# --------------------------------------------------------------------------
def _ver_tuple(v):
    """'v1.4.0' -> (1, 4, 0)，用于比较"""
    return tuple(int(x) for x in re.findall(r'\d+', v or '')[:4]) or (0,)


def check_update(current_version, use_cache=True):
    """查 GitHub Releases 最新版并与当前版本比对

    返回 dict：
      {ok, current, latest, has_update, notes, url, published_at, error}
    """
    now = time.time()
    if use_cache:
        with _check_lock:
            if _check_cache['data'] and now - _check_cache['at'] < 300:
                return _check_cache['data']

    result = {'ok': False, 'current': current_version, 'latest': '',
              'has_update': False, 'notes': '', 'url': '', 'published_at': '',
              'error': ''}
    try:
        import urllib.request
        req = urllib.request.Request(
            'https://api.github.com/repos/%s/releases/latest' % GITHUB_REPO,
            headers={'User-Agent': '115SavePro/%s' % current_version,
                     'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode('utf-8'))
        latest = (data.get('tag_name') or '').lstrip('v')
        result.update({
            'ok': True,
            'latest': latest,
            'has_update': _ver_tuple(latest) > _ver_tuple(current_version),
            'notes': data.get('body') or '',
            'url': data.get('html_url') or '',
            'published_at': data.get('published_at') or '',
        })
    except Exception as e:  # noqa: BLE001
        # 容器访问 GitHub API 失败很常见（网络受限），给出可操作提示
        result['error'] = ('查询 GitHub 失败：%s。'
                           '可在浏览器打开 https://github.com/%s/releases 手动查看。'
                           % (str(e)[:120], GITHUB_REPO))

    if result['ok']:
        with _check_lock:
            _check_cache['at'] = now
            _check_cache['data'] = result
    return result


# --------------------------------------------------------------------------
# 一键更新
# --------------------------------------------------------------------------
def update_capability():
    """检查「一键更新」当前是否可用，返回 (可用?, 说明, 环境信息)

    不可用时给出**具体原因**，前端据此告诉用户缺什么、怎么补。
    """
    ok, detail = socket_available()
    if not ok:
        return False, detail, {}

    try:
        info = self_info()
    except Exception as e:  # noqa: BLE001
        return False, '读取自身容器信息失败：%s' % str(e)[:120], {}

    if not info['workdir']:
        return False, ('这不是一个 docker compose 项目创建的容器'
                       '（缺少 com.docker.compose.project.working_dir 标签），'
                       '无法用 compose 安全重建，请手动更新'), info

    if not os.path.isdir('/app/config'):
        return False, 'config 目录未挂载，无法记录更新日志', info

    return True, '就绪（Docker %s，项目目录 %s）' % (detail, info['workdir']), info


def _assistant_script(workdir):
    """助手容器里执行的脚本

    日志同时写到 config/update.log —— 它挂载在宿主机项目目录下，
    新容器起来后前端能读到，用户就能看到"上次更新做了什么"。
    """
    return (
        "LOG=/work/config/update.log\n"
        "exec >> \"$LOG\" 2>&1\n"
        "echo \"\"\n"
        "echo \"==== $(date '+%Y-%m-%d %H:%M:%S') 开始更新 ====\"\n"
        "echo \"[0/2] 等待旧容器退出…\"\n"
        "sleep 3\n"
        "cd /work || { echo '进入项目目录失败'; exit 1; }\n"
        "# 兼容 compose v2 插件 与 独立的 docker-compose v1\n"
        "if docker compose version >/dev/null 2>&1; then\n"
        "  COMPOSE='docker compose'\n"
        "elif command -v docker-compose >/dev/null 2>&1; then\n"
        "  COMPOSE='docker-compose'\n"
        "else\n"
        "  echo '错误：容器内既没有 compose 插件也没有 docker-compose，无法继续'\n"
        "  exit 1\n"
        "fi\n"
        "echo \"[1/2] $COMPOSE pull\"\n"
        "$COMPOSE pull\n"
        "echo \"[2/2] $COMPOSE up -d --force-recreate\"\n"
        "$COMPOSE up -d --force-recreate\n"
        "echo \"更新流程结束（退出码 $?）\"\n"
    )


def run_self_update():
    """启动更新助手容器，返回 (是否已启动, 说明)

    本函数**只负责把助手容器拉起来**，随后自己很快就会被助手停掉重建，
    所以这里不等待结果 —— 更新结果通过 config/update.log 与重启后的
    版本号体现。
    """
    ok, detail, info = update_capability()
    if not ok:
        return False, detail

    workdir = info['workdir']
    sock_host = info['sock_host_path']

    # 清掉上次可能残留的助手容器
    try:
        st, _ = _docker('GET', '/containers/%s/json' % ASSISTANT_NAME, timeout=15)
        if st == 200:
            _docker('DELETE', '/containers/%s?force=true' % ASSISTANT_NAME, timeout=30)
    except Exception:  # noqa: BLE001
        pass

    body = {
        'Image': ASSISTANT_IMAGE,
        'Entrypoint': ['sh', '-c', _assistant_script(workdir)],
        'WorkingDir': '/work',
        'Labels': {'115savepro.role': 'updater'},
        'HostConfig': {
            'Binds': [
                '%s:/var/run/docker.sock' % sock_host,
                '%s:/work' % workdir,
            ],
            'AutoRemove': True,
            'RestartPolicy': {'Name': 'no'},
        },
    }

    st, obj = _docker('POST', '/containers/create?name=%s' % ASSISTANT_NAME,
                      body, timeout=60)
    if st not in (200, 201):
        msg = obj.get('message') if isinstance(obj, dict) else str(obj)[:200]
        if 'No such image' in str(msg) or 'pull' in str(msg).lower():
            return False, ('助手镜像 %s 不存在且无法自动拉取，'
                           '请先执行：docker pull %s' % (ASSISTANT_IMAGE, ASSISTANT_IMAGE))
        return False, '创建助手容器失败：HTTP %s %s' % (st, str(msg)[:200])

    aid = obj.get('Id') if isinstance(obj, dict) else ''
    st2, obj2 = _docker('POST', '/containers/%s/start' % aid, timeout=60)
    if st2 not in (204, 304):
        msg = obj2.get('message') if isinstance(obj2, dict) else str(obj2)[:200]
        return False, '启动助手容器失败：HTTP %s %s' % (st2, str(msg)[:200])

    logger.info('已启动更新助手容器 %s，项目目录 %s', aid[:12], workdir)
    return True, ('更新已开始。助手会先等 3 秒（让本页面收到响应），'
                  '然后拉取新镜像并重建容器 —— 期间本页面会短暂断线，'
                  '请稍等 20~60 秒后刷新。')


# --------------------------------------------------------------------------
# 更新日志
# --------------------------------------------------------------------------
def read_update_log(tail_chars=6000):
    """读取上次更新的输出（由助手容器写进 config/update.log）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'config', 'update.log')
    if not os.path.exists(path):
        return ''
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()[-tail_chars:]
    except Exception as e:  # noqa: BLE001
        return '读取更新日志失败：%s' % e


def clear_update_log():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'config', 'update.log')
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:  # noqa: BLE001
        return False

# -*- coding: utf-8 -*-
"""转存历史 + 应用级配置存储（SQLite）

单独放 SQLite 的原因：config.json 每次转存会被进度回写上百次，多个写入者互相覆盖，
放在里面的数据（历史记录、QMS 对接配置/连接列表）会被吃掉。
"""
import json
import os
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(BASE_DIR, 'config', 'history.db')
_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    conn = sqlite3.connect(_DB, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _init():
    with _lock:
        conn = _connect()
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_uid TEXT,
                order_no INTEGER,
                start_time TEXT,
                end_time TEXT,
                success INTEGER,
                message TEXT,
                file_count INTEGER,
                record TEXT
            )'''
        )
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS app_kv (
                k TEXT PRIMARY KEY,
                v TEXT,
                updated_at TEXT
            )'''
        )
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS qms_trigger_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                task_name TEXT,
                qms_id INTEGER,
                trigger_at TEXT,
                success INTEGER,
                message TEXT,
                source TEXT
            )'''
        )
        # 离线下载完成通知去重：记录已经因「下载完成」触发过 QMS 的任务 hash
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS offline_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                info_hash TEXT,
                name TEXT,
                status INTEGER,
                event_at TEXT,
                message TEXT
            )'''
        )
        conn.commit()
        conn.close()


# ---------------- 转存历史 ----------------

def record_task_history(task_uid=None, order=None, record=None, keep=10):
    """写入一条转存历史，并只保留每个任务最近 keep 条"""
    _init()
    record = record or {}
    with _lock:
        conn = _connect()
        conn.execute(
            'INSERT INTO task_history (task_uid, order_no, start_time, end_time, success, message, file_count, record)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (
                str(task_uid or ''),
                int(order or 0),
                record.get('start_time', ''),
                record.get('end_time', ''),
                1 if record.get('success') else 0,
                record.get('message', ''),
                int(record.get('file_count', 0)),
                json.dumps(record, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.execute(
            '''DELETE FROM task_history WHERE task_uid=? AND id NOT IN (
                SELECT id FROM task_history WHERE task_uid=? ORDER BY id DESC LIMIT ?)''',
            (str(task_uid or ''), str(task_uid or ''), int(keep)),
        )
        conn.commit()
        conn.close()


def get_task_history(task_uid=None, order=None, limit=10):
    """读取某个任务的转存历史（新的在前）"""
    _init()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            'SELECT record FROM task_history WHERE task_uid=? ORDER BY id DESC LIMIT ?',
            (str(task_uid or ''), int(limit)),
        )
        rows = []
        for r in cur.fetchall():
            try:
                rows.append(json.loads(r[0]))
            except (TypeError, ValueError):
                pass
        conn.close()
        return rows


def get_all_history(limit=100):
    """读取全部转存历史（新的在前），用于日志总览页"""
    _init()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            'SELECT record FROM task_history ORDER BY id DESC LIMIT ?', (int(limit),)
        )
        rows = []
        for r in cur.fetchall():
            try:
                rows.append(json.loads(r[0]))
            except (TypeError, ValueError):
                pass
        conn.close()
        return rows


# ---------------- 应用级配置（键值对） ----------------

def get_kv(key, default=None):
    """读一个应用级配置项（JSON 反序列化）"""
    _init()
    with _lock:
        conn = _connect()
        cur = conn.execute('SELECT v FROM app_kv WHERE k=?', (str(key),))
        row = cur.fetchone()
        conn.close()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def set_kv(key, value):
    """写一个应用级配置项（JSON 序列化，覆盖写）"""
    _init()
    with _lock:
        conn = _connect()
        conn.execute(
            'INSERT INTO app_kv (k, v, updated_at) VALUES (?,?,?) '
            'ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at',
            (str(key), json.dumps(value, ensure_ascii=False), time.strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()


def delete_kv(key):
    _init()
    with _lock:
        conn = _connect()
        conn.execute('DELETE FROM app_kv WHERE k=?', (str(key),))
        conn.commit()
        conn.close()


# ---------------- QMediaSync 触发日志 ----------------

def record_qms_log(link_id, task_name='', qms_id=None, success=False, message='', source='manual', keep=30):
    """记一条刮削触发日志，每条连接只保留最近 keep 条"""
    _init()
    link_id = str(link_id or '')
    with _lock:
        conn = _connect()
        conn.execute(
            'INSERT INTO qms_trigger_log (link_id, task_name, qms_id, trigger_at, success, message, source)'
            ' VALUES (?,?,?,?,?,?,?)',
            (
                link_id,
                str(task_name or ''),
                int(qms_id) if str(qms_id or '').isdigit() else None,
                time.strftime('%Y-%m-%d %H:%M:%S'),
                1 if success else 0,
                str(message or ''),
                str(source or 'manual'),
            ),
        )
        conn.commit()
        conn.execute(
            '''DELETE FROM qms_trigger_log WHERE link_id=? AND id NOT IN (
                SELECT id FROM qms_trigger_log WHERE link_id=? ORDER BY id DESC LIMIT ?)''',
            (link_id, link_id, int(keep)),
        )
        conn.commit()
        conn.close()


def get_qms_logs(link_id, limit=30):
    """读某条连接的触发日志（新的在前）"""
    _init()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            'SELECT trigger_at, success, message, source, qms_id FROM qms_trigger_log'
            ' WHERE link_id=? ORDER BY id DESC LIMIT ?',
            (str(link_id or ''), int(limit)),
        )
        rows = [
            {
                'trigger_at': r[0],
                'success': bool(r[1]),
                'message': r[2],
                'source': r[3],
                'qms_id': r[4],
            }
            for r in cur.fetchall()
        ]
        conn.close()
        return rows


# ---------------- 离线下载完成事件去重 ----------------

def offline_already_notified(info_hash):
    """判断这个离线任务是否已经因「下载完成」通知过"""
    _init()
    with _lock:
        conn = _connect()
        cur = conn.execute('SELECT 1 FROM offline_log WHERE info_hash=? LIMIT 1', (str(info_hash),))
        row = cur.fetchone()
        conn.close()
    return bool(row)


def record_offline_event(info_hash, name='', status=0, message=''):
    _init()
    with _lock:
        conn = _connect()
        conn.execute(
            'INSERT INTO offline_log (info_hash, name, status, event_at, message) VALUES (?,?,?,?,?)',
            (str(info_hash or ''), str(name or ''), int(status or 0),
             time.strftime('%Y-%m-%d %H:%M:%S'), str(message or '')),
        )
        conn.commit()
        # 只保留最近 2000 条
        conn.execute(
            'DELETE FROM offline_log WHERE id NOT IN (SELECT id FROM offline_log ORDER BY id DESC LIMIT 2000)'
        )
        conn.commit()
        conn.close()

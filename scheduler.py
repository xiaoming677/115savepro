# -*- coding: utf-8 -*-
"""定时调度

- 全局定时：config.cron.default_schedule（多个 cron，任一触发即跑一遍「没有单独定时的任务」）
- 单任务定时：任务自身的 cron 字段
- 空间告警：config.quota_alert.check_schedule
- 离线下载完成巡检：config.offline.watch_schedule

每轮执行完成后：
    1. 写转存历史（SQLite）
    2. 有新文件 → 触发绑定的 QMS 刮削
    3. 攒够 notification_delay 秒后合并推一条通知
"""
import time
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import notify as notifier
import qms_client
from history_db import record_task_history

TZ = 'Asia/Shanghai'


class TaskScheduler:
    def __init__(self, storage):
        self.storage = storage
        self.scheduler = None
        self._notify_buffer = []
        self._notify_timer = None
        self._notify_lock = threading.Lock()
        self._running = False

    # ---------------- 生命周期 ----------------

    def start(self):
        if self.scheduler:
            return
        cfg = self.storage.config
        sched_cfg = cfg.get('scheduler') or {}
        self.scheduler = BackgroundScheduler(
            timezone=TZ,
            job_defaults={
                'misfire_grace_time': int(sched_cfg.get('misfire_grace_time') or 3600),
                'coalesce': sched_cfg.get('coalesce', True) is not False,
                'max_instances': int(sched_cfg.get('max_instances') or 1),
            },
        )
        self.scheduler.start()
        self._running = True
        logger.info('调度器已启动')
        self.sync_jobs()
        self._add_quota_job()
        self._add_offline_watch_job()

    def stop(self):
        if self.scheduler:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self.scheduler = None
        self._running = False
        logger.info('调度器已停止')

    # ---------------- 任务同步 ----------------

    def sync_jobs(self):
        """按当前 config 重建所有任务 job（幂等）"""
        if not self.scheduler:
            return
        cfg = self.storage.config
        sched = self.scheduler

        # 清掉旧的 job
        for job in sched.get_jobs():
            if job.id.startswith(('task:', 'default-group')):
                try:
                    sched.remove_job(job.id)
                except Exception:  # noqa: BLE001
                    pass

        # 全局定时组
        schedules = (cfg.get('cron') or {}).get('default_schedule') or ['0 10 * * *']
        if isinstance(schedules, str):
            schedules = [schedules]
        for i, expr in enumerate(schedules):
            expr = (expr or '').strip()
            if not expr:
                continue
            try:
                trigger = CronTrigger.from_crontab(expr, timezone=TZ)
            except ValueError as e:
                logger.error('全局定时表达式无效 %s：%s' % (expr, e))
                continue
            sched.add_job(self._run_default_group, trigger=trigger,
                          id='default-group:%d' % i, replace_existing=True,
                          name='全局定时 %s' % expr)

        # 单任务定时
        for task in self.storage.list_tasks():
            if not task.get('enabled', True):
                continue
            expr = (task.get('cron') or '').strip()
            if not expr:
                continue
            try:
                trigger = CronTrigger.from_crontab(expr, timezone=TZ)
            except ValueError as e:
                logger.error('任务「%s」的定时表达式无效 %s：%s' % (task.get('name'), expr, e))
                continue
            sched.add_job(
                self._run_single_task_job, trigger=trigger, args=[task['task_uid']],
                id='task:%s' % task['task_uid'], replace_existing=True,
                name='任务 %s' % (task.get('name') or task['task_uid']),
            )
        logger.info('调度任务已同步，当前 job 数：%d' % len(sched.get_jobs()))

    def _add_quota_job(self):
        if not self.scheduler:
            return
        cfg = (self.storage.config.get('quota_alert') or {})
        try:
            self.scheduler.remove_job('quota-check')
        except Exception:  # noqa: BLE001
            pass
        if not cfg.get('enabled'):
            return
        expr = (cfg.get('check_schedule') or '0 0 * * *').strip()
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=TZ)
        except ValueError as e:
            logger.error('空间巡检表达式无效：%s' % e)
            return
        self.scheduler.add_job(self.check_quota, trigger=trigger,
                               id='quota-check', replace_existing=True, name='空间巡检')

    def _add_offline_watch_job(self):
        if not self.scheduler:
            return
        cfg = (self.storage.config.get('offline') or {})
        try:
            self.scheduler.remove_job('offline-watch')
        except Exception:  # noqa: BLE001
            pass
        expr = (cfg.get('watch_schedule') or '*/5 * * * *').strip()
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=TZ)
        except ValueError as e:
            logger.error('离线巡检表达式无效：%s' % e)
            return
        self.scheduler.add_job(self.check_offline_done, trigger=trigger,
                               id='offline-watch', replace_existing=True, name='离线下载巡检')

    def get_jobs(self):
        if not self.scheduler:
            return []
        out = []
        for job in self.scheduler.get_jobs():
            nxt = getattr(job, 'next_run_time', None)
            out.append({'id': job.id, 'name': job.name,
                        'next_run': nxt.strftime('%Y-%m-%d %H:%M:%S') if nxt else '',
                        'trigger': str(job.trigger)})
        out.sort(key=lambda x: x.get('next_run') or 'zzz')
        return out

    # ---------------- 执行 ----------------

    def _run_default_group(self):
        """全局定时：跑所有「启用 且 没有单独 cron」的任务"""
        tasks = [t for t in self.storage.list_tasks()
                 if t.get('enabled', True) and not (t.get('cron') or '').strip()]
        if not tasks:
            logger.info('全局定时触发：没有需要执行的任务')
            return
        self.run_tasks(tasks, source='定时(全局)')

    def _run_single_task_job(self, task_uid):
        task = self.storage.get_task_by_uid(task_uid)
        if not task:
            logger.warning('定时触发时任务已不存在：%s' % task_uid)
            return
        if not task.get('enabled', True):
            logger.info('任务「%s」已停用，跳过' % task.get('name'))
            return
        self.run_tasks([task], source='定时')

    def run_tasks(self, tasks, source='手动'):
        """执行一批任务，返回每项结果"""
        results = []
        for task in tasks or []:
            results.append(self.run_task(task, source=source))
        if results:
            self._buffer_notification(results)
        return results

    def run_task(self, task, source='手动'):
        """执行单个任务：转存 → 记历史 → 触发 QMS"""
        task = self.storage.normalize_task(task)
        task_uid = task['task_uid']
        name = task.get('name') or task.get('url') or task_uid
        start_ts = time.strftime('%Y-%m-%d %H:%M:%S')
        logger.info('[%s] 开始执行任务「%s」' % (source, name))
        self.storage.update_task_status(task_uid, 'running', '正在执行')

        record = {
            'task_uid': task_uid, 'task_name': name, 'source': source,
            'start_time': start_ts, 'end_time': '', 'success': False,
            'message': '', 'file_count': 0, 'new_items': [], 'skipped': [],
            'save_dir': task.get('save_dir'), 'log': [],
            'config_snapshot': {
                'save_dir': task.get('save_dir'),
                'compare_path': task.get('compare_path'),
                'regex_pattern': task.get('regex_pattern'),
                'regex_replace': task.get('regex_replace'),
                'dedupe_mode': task.get('dedupe_mode'),
                'exclude_files': task.get('exclude_files'),
                'transfer_file_ids': task.get('transfer_file_ids'),
            },
        }

        log_lines = []

        def progress(msg, level='INFO'):
            log_lines.append('[%s] %s' % (level, msg))

        try:
            result = self.storage.transfer_share(task, progress=progress)
            record.update({
                'success': bool(result.get('success')),
                'message': result.get('message') or '',
                'file_count': int(result.get('file_count') or 0),
                'new_items': result.get('new_items') or [],
                'skipped': result.get('skipped') or [],
                'target_cid': result.get('target_cid'),
            })
            self.storage.update_task_status(task_uid, 'success' if record['success'] else 'failed',
                                            record['message'], record['file_count'])
            item = {'task': task, **{k: record[k] for k in
                                     ('success', 'message', 'file_count', 'new_items', 'save_dir', 'skipped')}}

            # 有新文件 → 触发 QMS
            if record['file_count'] > 0:
                try:
                    summary = qms_client.trigger_after_transfer(task, record['file_count'])
                    if summary:
                        log_lines.append('[INFO] QMS：%s' % summary)
                        record['qms_result'] = summary
                except Exception as e:  # noqa: BLE001
                    log_lines.append('[ERROR] QMS 触发异常：%s' % e)
                    record['qms_result'] = '异常：%s' % e

        except Exception as e:  # noqa: BLE001
            logger.exception('任务「%s」执行失败' % name)
            record.update({'success': False, 'message': str(e)})
            log_lines.append('[ERROR] %s' % e)
            self.storage.update_task_status(task_uid, 'failed', str(e), 0)
            item = {'task': task, 'success': False, 'message': str(e),
                    'file_count': 0, 'new_items': [], 'save_dir': task.get('save_dir'), 'skipped': []}

        record['end_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
        record['log'] = log_lines
        try:
            record_task_history(task_uid=task_uid, order=task.get('order'), record=record, keep=10)
        except Exception as e:  # noqa: BLE001
            logger.error('写转存历史失败：%s' % e)
        item['record'] = record
        logger.info('[%s] 任务「%s」结束：%s' % (source, name, record['message']))
        return item

    # ---------------- 通知合并 ----------------

    def _buffer_notification(self, results):
        with self._notify_lock:
            self._notify_buffer.extend(results)
            delay = int((self.storage.config.get('notify') or {}).get('notification_delay') or 30)
            if self._notify_timer:
                self._notify_timer.cancel()
            self._notify_timer = threading.Timer(max(delay, 1), self._flush_notification)
            self._notify_timer.daemon = True
            self._notify_timer.start()

    def _flush_notification(self):
        with self._notify_lock:
            results = self._notify_buffer
            self._notify_buffer = []
        if not results:
            return
        content = notifier.build_transfer_content(results)
        if not content.strip():
            return
        cfg = self.storage.config.get('notify') or {}
        title = '115 转存完成'
        try:
            notifier.send(title, content, cfg)
        except Exception as e:  # noqa: BLE001
            logger.error('发送通知失败：%s' % e)

    # ---------------- 空间巡检 ----------------

    def check_quota(self):
        cfg = self.storage.config.get('quota_alert') or {}
        if not cfg.get('enabled'):
            return
        threshold = float(cfg.get('threshold_percent') or 90)
        try:
            info = self.storage.space_info()
        except Exception as e:  # noqa: BLE001
            logger.error('空间巡检失败：%s' % e)
            return
        percent = info.get('space_percent') or 0
        logger.info('空间巡检：已用 %s%%（阈值 %s%%）' % (percent, threshold))
        if percent >= threshold:
            content = ('115 网盘空间告警\n已用：%s / %s（%s%%）\n阈值：%s%%'
                       % (info.get('space_used_text'), info.get('space_total_text'), percent, threshold))
            try:
                notifier.send('115 网盘空间告警', content, self.storage.config.get('notify') or {})
            except Exception as e:  # noqa: BLE001
                logger.error('发送空间告警失败：%s' % e)

    # ---------------- 离线下载巡检 ----------------

    def check_offline_done(self):
        """巡检已完成但还没处理过的离线任务 → 触发 QMS + 通知"""
        from history_db import offline_already_notified, record_offline_event
        qms_cfg = qms_client.load_cfg()
        if not (qms_cfg.get('enabled') and qms_cfg.get('auto_trigger', True)):
            return
        if not (self.storage.config.get('offline') or {}).get('auto_trigger_qms', True):
            return
        try:
            page = self.storage.offline_list(page=1, page_size=100, stat=11)   # 11 = 已完成
        except Exception as e:  # noqa: BLE001
            logger.warning('离线下载巡检失败：%s' % e)
            return
        done = []
        for t in page.get('items') or []:
            h = t.get('info_hash') or ''
            if not h or offline_already_notified(h):
                continue
            done.append(t)
        if not done:
            return
        logger.info('离线下载巡检：发现 %d 个新完成的任务' % len(done))
        for t in done:
            record_offline_event(t.get('info_hash'), t.get('name'), 11, '已完成')
        names = [t.get('name') or t.get('info_hash') for t in done]
        summary = ''
        try:
            summary = qms_client.trigger_offline_done(
                task_name='离线下载（%d 个任务）' % len(done)) or ''
        except Exception as e:  # noqa: BLE001
            summary = '触发异常：%s' % e
        lines = ['✅ 115 离线下载完成 %d 个任务：' % len(done)]
        for n in names[:30]:
            lines.append('├── %s' % n)
        if summary:
            lines.append('')
            lines.append('QMS：%s' % summary)
        try:
            notifier.send('115 离线下载完成', '\n'.join(lines),
                          self.storage.config.get('notify') or {})
        except Exception as e:  # noqa: BLE001
            logger.error('发送离线完成通知失败：%s' % e)

# -*- coding: utf-8 -*-
"""通知推送

支持渠道（按配置里的 direct_fields 自动启用未留空的项）：
    Bark / PushPlus / 钉钉机器人 / 飞书机器人 / 企业微信机器人 / 企业微信应用
    Telegram / Server酱 / ntfy / Gotify / PushDeer / SMTP 邮件 / 自定义 Webhook / 控制台
"""
import base64
import hashlib
import hmac
import json
import smtplib
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
from loguru import logger

TIMEOUT = 15

# 供前端「系统设置-通知」渲染的字段说明
FIELD_META = [
    {'key': 'BARK_PUSH', 'label': 'Bark', 'hint': 'iOS 推送，填完整地址，如 https://api.day.app/你的key'},
    {'key': 'PUSH_PLUS_TOKEN', 'label': 'PushPlus', 'hint': '微信推送，填 token'},
    {'key': 'PUSH_PLUS_USER', 'label': 'PushPlus 群组', 'hint': '可选，填群组编码'},
    {'key': 'DD_BOT_TOKEN', 'label': '钉钉机器人', 'hint': '填 access_token；加签填 DD_BOT_SECRET'},
    {'key': 'DD_BOT_SECRET', 'label': '钉钉加签密钥', 'hint': '可选，机器人安全设置为「加签」时填'},
    {'key': 'FSKEY', 'label': '飞书机器人', 'hint': '填机器人的 webhook key'},
    {'key': 'QYWX_KEY', 'label': '企业微信机器人', 'hint': '群机器人 webhook 的 key'},
    {'key': 'QYWX_AM', 'label': '企业微信应用', 'hint': '格式：corpid,corpsecret,agentid'},
    {'key': 'TG_BOT_TOKEN', 'label': 'Telegram Bot Token', 'hint': '如 123456:ABC...'},
    {'key': 'TG_CHAT_ID', 'label': 'Telegram Chat ID', 'hint': '接收消息的会话 id'},
    {'key': 'PUSH_KEY', 'label': 'Server酱', 'hint': 'SendKey，形如 SCT…'},
    {'key': 'NTFY_URL', 'label': 'ntfy', 'hint': '如 https://ntfy.sh/你的主题'},
    {'key': 'GOTIFY_URL', 'label': 'Gotify 地址', 'hint': '如 https://gotify.example.com'},
    {'key': 'GOTIFY_TOKEN', 'label': 'Gotify Token', 'hint': '应用 token'},
    {'key': 'PUSHDEER_KEY', 'label': 'PushDeer', 'hint': 'PushKey'},
    {'key': 'SMTP_SERVER', 'label': 'SMTP 服务器', 'hint': '如 smtp.qq.com'},
    {'key': 'SMTP_PORT', 'label': 'SMTP 端口', 'hint': '默认 465（SSL）'},
    {'key': 'SMTP_EMAIL', 'label': '发件邮箱', 'hint': '如 xxx@qq.com'},
    {'key': 'SMTP_PASSWORD', 'label': '邮箱授权码', 'hint': '不是登录密码，是 SMTP 授权码'},
    {'key': 'SMTP_TO', 'label': '收件邮箱', 'hint': '留空则发给自己'},
    {'key': 'WEBHOOK_URL', 'label': '自定义 Webhook', 'hint': 'POST 地址，GET 也支持'},
    {'key': 'WEBHOOK_METHOD', 'label': 'Webhook 方法', 'hint': 'GET / POST，默认 POST'},
    {'key': 'WEBHOOK_BODY', 'label': 'Webhook 请求体', 'hint': '支持 $title / $content 占位符'},
]


def _post(url, **kw):
    kw.setdefault('timeout', TIMEOUT)
    resp = requests.request(**{'method': kw.pop('method', 'POST'), 'url': url, **kw})
    return resp


# ---------------- 各渠道 ----------------

def bark(title, content, cfg):
    url = cfg['BARK_PUSH'].rstrip('/')
    if '://' not in url:
        url = 'https://api.day.app/' + url
    r = requests.get('%s/%s/%s' % (url, urllib.parse.quote(title), urllib.parse.quote(content)),
                     timeout=TIMEOUT)
    return r.text


def pushplus(title, content, cfg):
    body = {'token': cfg['PUSH_PLUS_TOKEN'], 'title': title, 'content': content, 'template': 'txt'}
    if cfg.get('PUSH_PLUS_USER'):
        body['topic'] = cfg['PUSH_PLUS_USER']
    return _post('https://www.pushplus.plus/send', data=json.dumps(body),
                 headers={'Content-Type': 'application/json'}).text


def dingtalk(title, content, cfg):
    token = cfg['DD_BOT_TOKEN']
    url = 'https://oapi.dingtalk.com/robot/send?access_token=%s' % token
    secret = cfg.get('DD_BOT_SECRET')
    if secret:
        ts = str(round(time.time() * 1000))
        sign_str = '%s\n%s' % (ts, secret)
        sign = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(secret.encode(), sign_str.encode(), digestmod=hashlib.sha256).digest()))
        url += '&timestamp=%s&sign=%s' % (ts, sign)
    body = {'msgtype': 'text', 'text': {'content': '%s\n\n%s' % (title, content)}}
    return _post(url, data=json.dumps(body), headers={'Content-Type': 'application/json'}).text


def feishu(title, content, cfg):
    url = 'https://open.feishu.cn/open-apis/bot/v2/hook/%s' % cfg['FSKEY']
    body = {'msg_type': 'text', 'content': {'text': '%s\n\n%s' % (title, content)}}
    return _post(url, data=json.dumps(body), headers={'Content-Type': 'application/json'}).text


def wecom_bot(title, content, cfg):
    url = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=%s' % cfg['QYWX_KEY']
    body = {'msgtype': 'text', 'text': {'content': '%s\n\n%s' % (title, content)}}
    return _post(url, data=json.dumps(body), headers={'Content-Type': 'application/json'}).text


def wecom_app(title, content, cfg):
    parts = [p.strip() for p in str(cfg['QYWX_AM']).split(',')]
    if len(parts) < 3:
        raise ValueError('企业微信应用配置格式应为 corpid,corpsecret,agentid')
    corpid, corpsecret, agentid = parts[0], parts[1], parts[2]
    touser = parts[3] if len(parts) > 3 else '@all'
    token_resp = requests.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
                              params={'corpid': corpid, 'corpsecret': corpsecret},
                              timeout=TIMEOUT).json()
    token = token_resp.get('access_token')
    if not token:
        raise ValueError('获取企业微信 token 失败：%s' % token_resp.get('errmsg'))
    body = {'touser': touser, 'msgtype': 'text', 'agentid': int(agentid),
            'text': {'content': '%s\n\n%s' % (title, content)}}
    return _post('https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % token,
                 data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                 headers={'Content-Type': 'application/json'}).text


def telegram(title, content, cfg):
    url = 'https://api.telegram.org/bot%s/sendMessage' % cfg['TG_BOT_TOKEN']
    body = {'chat_id': cfg['TG_CHAT_ID'], 'text': '%s\n\n%s' % (title, content)}
    return _post(url, data=json.dumps(body), headers={'Content-Type': 'application/json'}).text


def serverchan(title, content, cfg):
    key = cfg['PUSH_KEY']
    url = 'https://sctapi.ftqq.com/%s.send' % key
    return _post(url, data={'title': title, 'desp': content}).text


def ntfy(title, content, cfg):
    url = cfg['NTFY_URL'].rstrip('/')
    headers = {'Title': urllib.parse.quote(title.encode('utf-8')), 'Content-Type': 'text/plain'}
    return _post(url, data=content.encode('utf-8'), headers=headers).text


def gotify(title, content, cfg):
    url = '%s/message?token=%s' % (cfg['GOTIFY_URL'].rstrip('/'), cfg['GOTIFY_TOKEN'])
    body = {'title': title, 'message': content, 'priority': 5}
    return _post(url, data=json.dumps(body), headers={'Content-Type': 'application/json'}).text


def pushdeer(title, content, cfg):
    if str(cfg['PUSHDEER_KEY']).startswith('http'):
        url = cfg['PUSHDEER_KEY']
    else:
        url = 'https://api2.pushdeer.com/message/push'
    return _post(url, data={'pushkey': cfg['PUSHDEER_KEY'], 'text': title, 'desp': content}).text


def smtp(title, content, cfg):
    server = cfg['SMTP_SERVER']
    port = int(cfg.get('SMTP_PORT') or 465)
    sender = cfg['SMTP_EMAIL']
    password = cfg['SMTP_PASSWORD']
    to = cfg.get('SMTP_TO') or sender
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['From'] = formataddr((str(Header('115SavePro', 'utf-8')), sender))
    msg['To'] = to
    msg['Subject'] = Header(title, 'utf-8')
    if port == 465:
        server_obj = smtplib.SMTP_SSL(server, port, timeout=TIMEOUT)
    else:
        server_obj = smtplib.SMTP(server, port, timeout=TIMEOUT)
        server_obj.starttls()
    server_obj.login(sender, password)
    server_obj.sendmail(sender, to.split(','), msg.as_string())
    server_obj.quit()
    return 'ok'


def custom_webhook(title, content, cfg):
    url = cfg['WEBHOOK_URL']
    method = (cfg.get('WEBHOOK_METHOD') or 'POST').upper()
    body_tpl = cfg.get('WEBHOOK_BODY') or ''
    if body_tpl:
        text = body_tpl.replace('$title', title).replace('$content', content)
        try:
            data = json.loads(text)
        except ValueError:
            data = text
    else:
        data = {'title': title, 'content': content}
    if method == 'GET':
        return requests.get(url, params={'title': title, 'content': content}, timeout=TIMEOUT).text
    if isinstance(data, str):
        return _post(url, data=data.encode('utf-8'),
                     headers={'Content-Type': 'application/json'}).text
    return _post(url, data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                 headers={'Content-Type': 'application/json'}).text


HANDLERS = {
    'BARK_PUSH': bark,
    'PUSH_PLUS_TOKEN': pushplus,
    'DD_BOT_TOKEN': dingtalk,
    'FSKEY': feishu,
    'QYWX_KEY': wecom_bot,
    'QYWX_AM': wecom_app,
    'TG_BOT_TOKEN': telegram,
    'PUSH_KEY': serverchan,
    'NTFY_URL': ntfy,
    'GOTIFY_URL': gotify,
    'PUSHDEER_KEY': pushdeer,
    'SMTP_SERVER': smtp,
    'WEBHOOK_URL': custom_webhook,
}

# 渠道判定：只要该键存在且非空就认为启用了这个渠道
TRIGGER_KEYS = ['BARK_PUSH', 'PUSH_PLUS_TOKEN', 'DD_BOT_TOKEN', 'FSKEY', 'QYWX_KEY',
                'QYWX_AM', 'TG_BOT_TOKEN', 'PUSH_KEY', 'NTFY_URL', 'GOTIFY_URL',
                'PUSHDEER_KEY', 'SMTP_SERVER', 'WEBHOOK_URL']


def send(title, content, notify_config=None, force=False):
    """按配置发送通知，返回 {渠道: 结果}

    force=True 用于「发送测试」，忽略「启用通知」开关，只要填了渠道就发。
    """
    notify_config = notify_config or {}
    cfg = notify_config.get('direct_fields') or notify_config
    results = {}
    if not force and not notify_config.get('enabled', False):
        logger.info('通知未启用，跳过')
        return results
    if not any(cfg.get(k) for k in TRIGGER_KEYS):
        logger.info('没有配置任何通知渠道，跳过')
        return results
    for key in TRIGGER_KEYS:
        if not cfg.get(key):
            continue
        handler = HANDLERS.get(key)
        if not handler:
            continue
        # 部分渠道需要组合字段齐全
        if key == 'TG_BOT_TOKEN' and not cfg.get('TG_CHAT_ID'):
            continue
        if key == 'GOTIFY_URL' and not cfg.get('GOTIFY_TOKEN'):
            continue
        if key == 'SMTP_SERVER' and not (cfg.get('SMTP_EMAIL') and cfg.get('SMTP_PASSWORD')):
            continue
        try:
            handler(title, content, cfg)
            results[key] = 'ok'
            logger.info('通知已发送：%s' % key)
        except Exception as e:  # noqa: BLE001
            results[key] = '失败：%s' % e
            logger.error('通知发送失败 %s：%s' % (key, e))
    return results


def build_transfer_content(results):
    """把一轮转存结果拼成通知正文"""
    lines = []
    for item in results or []:
        task = item.get('task') or {}
        name = task.get('name') or task.get('url') or '未知任务'
        if item.get('success'):
            new_items = item.get('new_items') or []
            if not new_items:
                continue
            lines.append('✅《%s》新增 %d 项：' % (name, len(new_items)))
            lines.append(item.get('save_dir') or '')
            names = [n.get('name') or '' for n in new_items][:50]
            for i, n in enumerate(names):
                prefix = '└── ' if i == len(names) - 1 else '├── '
                lines.append(prefix + n)
            if len(new_items) > 50:
                lines.append('└── …… 其余 %d 项已省略' % (len(new_items) - 50))
            lines.append('')
        else:
            lines.append('❌《%s》：%s' % (name, item.get('message') or '未知错误'))
            lines.append('')
    return '\n'.join(lines).strip()

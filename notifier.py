#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notifier.py — 通知：日志 + 蜂鸣 + Webhook（企业微信机器人 / Bark / ServerChan 通用）

webhook 留空则只写日志。企业微信机器人格式：
  {"msgtype":"text","text":{"content":"标题\n内容"}}
"""

import json
import os
import time
import urllib.request

try:
    import winsound
except ImportError:          # 非 Windows 开发环境
    winsound = None


def _post_webhook(url, text):
    body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


class Notifier:
    def __init__(self, cfg):
        self.webhook = cfg.get("notify_webhook", "")
        self.beep = cfg.get("beep_on", True)
        self.log_file = cfg.get("log_file", "signer.log")

    def log(self, msg):
        line = "%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def notify(self, title, body=""):
        self.log("%s %s" % (title, body))
        if self.beep and winsound:
            try:
                winsound.Beep(880, 200)
            except Exception:
                pass
        if self.webhook:
            try:
                _post_webhook(self.webhook, "%s\n%s" % (title, body))
            except Exception as e:
                self.log("webhook 发送失败: %s" % e)

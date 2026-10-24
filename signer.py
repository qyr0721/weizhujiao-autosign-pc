#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signer.py — 微助教电脑端（单设备）自动签到 · 执行器 + 主循环

逻辑从手机端 tm_flow.sh 一对一移植，并修复其两个已知限制：
  - 多课并行：遍历 active_signs 数组，全部提交（手机端只签第一场）
  - 锁屏/安全锁：PC 无此问题

用法：
  python signer.py                 # 守护进程模式（默认）
  python signer.py --once          # 单轮轮询后退出（调试用）
  python signer.py --status        # 打印当前状态后退出
  python signer.py --refresh       # 立即强制取号一次后退出
  python signer.py --config x.json

依赖：pycryptodome（取号用）。HTTP 层只用标准库。
"""

import argparse
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request

import auth_refresher
import notifier as notifier_mod
import wechat_oid

API_BASE = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"
HERE = os.path.dirname(os.path.abspath(__file__))


class Signer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ntf = notifier_mod.Notifier(cfg)
        self.state_path = os.path.join(HERE, cfg.get("state_file", "state.json"))
        self.state = {"signed": [], "last_refresh": 0, "last_oid": "",
                      "signed_total": 0, "fail_streak": 0}
        self._load_state()
        self.oid = ""
        self.net_fail = 0
        self.sign_fail = {}
        self.last_refresh_attempt = 0
        self._oid_check = 0

    # ---------------------------------------------------------- 状态
    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                self.state.update(json.load(f))
        except Exception:
            pass

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---------------------------------------------------------- HTTP
    def _req(self, method, path, body=None, headers=None, timeout=12):
        url = API_BASE + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"User-Agent": UA, "Referer": REF}
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return -1, "NETERR %s" % e

    def poll_signs(self):
        return self._req("GET", "/student/active_signs", headers={"openId": self.oid})

    # ---------------------------------------------------------- 坐标
    def _jitter(self):
        d = self.cfg.get("jitter_deg", 0.00003)
        return (self.cfg["lat"] + (random.random() - 0.5) * d,
                self.cfg["lon"] + (random.random() - 0.5) * d)

    # ---------------------------------------------------------- 取号
    def in_class_hours(self):
        lo, hi = self.cfg.get("class_hours", [7, 23])
        return lo <= time.localtime().tm_hour < hi

    def need_refresh(self):
        return time.time() - self.state.get("last_refresh", 0) >= self.cfg.get("refresh_every", 660)

    def do_refresh(self, reason="定期续号", force=False):
        now = time.time()
        # 无论是否强制，距上次取号尝试不足 15 秒都不动（防止 401 原地打转）
        if now - self.last_refresh_attempt < 15:
            return False
        self.last_refresh_attempt = now
        if not force and not self.in_class_hours():
            self.ntf.log("非上课时段，跳过取号（%s）" % reason)
            return False
        self.ntf.log("取号：%s" % reason)
        try:
            ok, oid, msg = auth_refresher.refresh(self.cfg)
        except Exception as e:
            ok, oid, msg = False, None, "取号异常 %s" % e
        if ok:
            self.oid = oid
            self.state["last_refresh"] = time.time()
            self.state["last_oid"] = oid[:8] + "…" + oid[-8:]
            self._save_state()
            self.ntf.notify("取号成功", msg)
            return True
        self.ntf.notify("取号失败", msg)
        self.state["last_refresh"] = time.time() - self.cfg.get("refresh_every", 660) + 15
        self._save_state()
        return False

    def ensure_oid(self):
        """确保有一个可用 openid：优先内存提取（主路径），其次数据库路线"""
        if self.oid and time.time() - self._oid_check < 30:
            return True
        self._oid_check = time.time()
        # 1) 内存提取：微信 Cookies 数据库被独占锁定，内存是可靠读取路径
        try:
            import mem_oid
            hits = mem_oid.scan_openids()
            if hits:
                oid = hits[0][0]
                if oid != self.oid:
                    self.ntf.log("openid 就绪 %s…%s（内存提取，pid=%d）" % (oid[:8], oid[-8:], hits[0][1]))
                self.oid = oid
                self.state["last_refresh"] = max(self.state.get("last_refresh", 0), time.time() - 600)
                return True
        except Exception as e:
            self.ntf.log("内存提取失败: %s" % e)
        # 2) 数据库路线（兜底：数据库恰好未被锁定时可用）
        oid, why = wechat_oid.current_valid_openid()
        if oid:
            if oid != self.oid:
                self.ntf.log("openid 就绪 %s…%s（数据库）" % (oid[:8], oid[-8:]))
            self.oid = oid
            self.state["last_refresh"] = max(self.state.get("last_refresh", 0), time.time() - 600)
            return True
        return self.do_refresh("本地无有效 openid（%s）" % why, force=True)

    # ---------------------------------------------------------- 签到
    def sign_one(self, item):
        sign_id, course_id = item.get("signId"), item.get("courseId")
        body = {"courseId": course_id, "signId": sign_id}
        if item.get("isGPS", 0) and self.cfg.get("lat") is not None:
            lat, lon = self._jitter()
            body["lat"], body["lon"] = round(lat, 6), round(lon, 6)
        code, resp = self._req("POST", "/student-sign-in", body, headers={"openId": self.oid})
        if "signRank" in resp:
            self.state["signed_total"] += 1
            self.ntf.notify("签到成功", "signId=%s courseId=%s %s" % (sign_id, course_id, resp[:120]))
            return True
        if "repeat sign in" in resp or "已经签到" in resp or '"errorCode":305' in resp.replace(" ", ""):
            self.ntf.log("已签到（幂等）signId=%s" % sign_id)
            return True
        self.ntf.notify("签到失败", "signId=%s HTTP %s %s" % (sign_id, code, resp[:120]))
        return False

    def poll_once(self):
        if not self.ensure_oid():
            time.sleep(5)
            return
        code, resp = self.poll_signs()
        if code == -1:
            self.net_fail += 1
            wait = min(2 ** self.net_fail, 10)
            self.ntf.log("网络错误，%ds 后重试（连续 %d 次）" % (wait, self.net_fail))
            if self.net_fail >= 10:
                self.ntf.notify("网络异常", "连续 %d 次请求失败" % self.net_fail)
            time.sleep(wait)
            return
        self.net_fail = 0
        if code == 401 or "登录信息失效" in resp:
            self.ntf.log("401 会话过期，立即取号")
            self.do_refresh("401 触发", force=True)
            return
        if code != 200:
            self.ntf.log("active_signs HTTP %s %s" % (code, resp[:100]))
            return

        try:
            items = json.loads(resp)
        except Exception:
            self.ntf.log("active_signs 响应解析失败: %s" % resp[:100])
            return
        if not isinstance(items, list):
            return
        signed = set(self.state.get("signed", []))
        n_active = 0
        for item in items:
            sid = item.get("signId")
            if sid is None:
                continue
            n_active += 1
            if str(sid) in signed:
                continue
            if self.sign_one(item):
                signed.add(str(sid))
                self.state["signed"] = sorted(signed)[-500:]
                self._save_state()
            else:
                self.sign_fail[str(sid)] = self.sign_fail.get(str(sid), 0) + 1
                if self.sign_fail[str(sid)] >= 5:
                    self.ntf.notify("签到多次失败", "signId=%s 已失败 %d 次，下轮继续重试"
                                    % (sid, self.sign_fail[str(sid)]))
                    self.sign_fail[str(sid)] = 0
        if n_active == 0:
            self.ntf.log("无进行中的签到")

    # ---------------------------------------------------------- 主循环
    def run(self):
        self.ntf.notify("签到服务启动", "间隔 %ds / 续号周期 %ds / 上课时段 %s"
                        % (self.cfg.get("interval", 5), self.cfg.get("refresh_every", 660),
                           self.cfg.get("class_hours", [7, 23])))
        while True:
            try:
                if self.need_refresh() and self.in_class_hours():
                    self.do_refresh("定期续号")
                self.poll_once()
            except Exception as e:
                self.ntf.log("主循环异常: %s" % e)
                time.sleep(5)
            self._save_state()
            time.sleep(self.cfg.get("interval", 5))


def single_instance(port=47971):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    sg = Signer(cfg)

    if args.status:
        print(json.dumps(sg.state, ensure_ascii=False, indent=2))
        return
    if args.refresh:
        ok, oid, msg = auth_refresher.refresh(cfg)
        print(("成功：" if ok else "失败：") + msg)
        if ok:
            sg.oid, sg.state["last_refresh"] = oid, time.time()
            sg._save_state()
        return
    if args.once:
        sg.poll_once()
        print(json.dumps(sg.state, ensure_ascii=False, indent=2))
        return

    if not single_instance():
        print("已有实例在运行（端口 47971 被占用），退出")
        return
    sg.run()


if __name__ == "__main__":
    main()

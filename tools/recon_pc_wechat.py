#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC 微信 Cookie 库勘查（只读，Windows，零第三方依赖，Python 3.8+）

用途：定位微信内置浏览器（CEF）存储 teachermate 会话的位置，判定 openid 读取方式：
  A. 找到含 eyJvcGVuSWQ（base64 的 {"openId）明文的文件 -> 直接 grep 解码即可
  B. 只有加密值 -> 需按 Chromium v10 方案用同用户 DPAPI 解密钥
  C. 什么都没找到 -> 退用"复制链接"方式

安全说明：全程只读；不启动、不修改微信；sqlite 以 immutable 模式打开，不加锁。
"""

import os
import sqlite3
import sys
import time

# 微信常见安装/数据根（含 4.x 的 xwechat_files 改名）
CANDIDATE_ROOTS = [
    os.path.expandvars(r"%USERPROFILE%\Documents\xwechat_files"),
    os.path.expandvars(r"%USERPROFILE%\Documents\WeChat Files"),
    os.path.expandvars(r"%USERPROFILE%\Documents\WeChat Files\All Users"),
    os.path.expandvars(r"%APPDATA%\Tencent\WeChat"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tencent\WeChat"),
    os.path.expandvars(r"%PROGRAMFILES%\Tencent\WeChat"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Tencent\WeChat"),
    os.path.expandvars(r"%PROGRAMFILES%\Tencent\Weixin"),
]
SIG = b"eyJvcGVuSWQ"          # base64('{"openId') 的前缀
NAME_HINTS = ("cookie", "cookies", "session", "network", "xweb")
MAX_FILE = 200 * 1024 * 1024  # 跳过 >200MB 的文件
MAX_DEPTH = 7
TIME_BUDGET = 120             # 秒


def walk_bounded(root):
    root = os.path.abspath(root)
    base_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            yield os.path.join(dirpath, name)


def sniff_file(path):
    """返回 dict：是否含明文 openId、是否像 SQLite、cookie 表摘要"""
    info = {"plain_openid": False, "sqlite": False, "rows": []}
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE:
            return info
        with open(path, "rb") as f:
            head = f.read(16)
            if head.startswith(b"SQLite format 3"):
                info["sqlite"] = True
            chunk = f.read(4 * 1024 * 1024)
            if SIG in chunk:
                info["plain_openid"] = True
    except Exception:
        return info

    if info["sqlite"]:
        try:
            uri = "file:%s?immutable=1" % path.replace("?", "%3f").replace("#", "%23")
            con = sqlite3.connect(uri, uri=True, timeout=3)
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%cookie%'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables[:2]:
                try:
                    cur = con.execute("SELECT host_key, name, length(value) FROM %s "
                                      "WHERE host_key LIKE '%%teachermate%%' "
                                      "OR host_key LIKE '%%v18.teachermate%%' LIMIT 10" % t)
                    for host, cname, vlen in cur.fetchall():
                        info["rows"].append((t, host, cname, vlen))
                except Exception:
                    pass
            con.close()
        except Exception:
            pass
    return info


def main():
    print("PC 微信 Cookie 库勘查（只读）  %s" % time.strftime("%H:%M:%S"))
    t0 = time.time()
    hits = []
    for root in CANDIDATE_ROOTS:
        if not os.path.isdir(root):
            continue
        print("\n[扫描] %s" % root)
        n = 0
        for path in walk_bounded(root):
            if time.time() - t0 > TIME_BUDGET:
                print("    （超出 %ds 预算，停止深入扫描）" % TIME_BUDGET)
                break
            n += 1
            lower = path.lower()
            try:
                if not any(h in os.path.basename(lower) for h in NAME_HINTS):
                    continue
            except Exception:
                continue
            info = sniff_file(path)
            if info["plain_openid"] or info["sqlite"] or info["rows"]:
                tag = []
                if info["plain_openid"]:
                    tag.append("★明文openId")
                if info["sqlite"]:
                    tag.append("SQLite")
                if info["rows"]:
                    tag.append("含teachermate行")
                print("    [命中] %s  (%s)" % (path, ", ".join(tag) or "命名可疑"))
                for t, host, cname, vlen in info["rows"]:
                    print("           表%s: %s / %s (value %dB)" % (t, host, cname, vlen))
                hits.append(path)
        print("    （共查看 %d 个文件）" % n)

    print("\n" + "=" * 60)
    if not hits:
        print("未发现候选。可能原因：微信未登录/未启动过内置浏览器；"
              "或存储路径不在候选列表。请在微信里手动打开一次签到页后重跑。")
        print("提示：也可直接全盘搜文件名含 Cookies 的 sqlite：")
        print('  powershell -c "Get-ChildItem C:\\Users\\%USERNAME% -Recurse -Filter Cookies '
              '-ErrorAction SilentlyContinue | Select FullName,LastWriteTime"')
        sys.exit(1)
    print("候选文件 %d 个。下一步：" % len(hits))
    print("  1) 对含★明文openId的文件：grep 出 eyJvcGVuSWQ... 段，base64 解码取 openId（方式 A）")
    print("  2) 若只有加密值：按 Chromium v10 用同用户 DPAPI 解 Local State 密钥（方式 B）")
    print("  3) 打开一次签到页前后各跑一次本脚本，对比哪个文件 mtime/内容变化，即为真身")


if __name__ == "__main__":
    main()

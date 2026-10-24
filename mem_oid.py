#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mem_oid.py — 从微信进程内存直接提取微助教 openid（纯 ctypes，无注入无 Frida）

背景：授权链接在微信内置浏览器打开后，session（base64 的 {"openId":...}）会以
明文形式留在浏览器进程内存里；而磁盘上的 Cookies 数据库被微信独占锁定，
任何外部进程（包括同进程重开）都读不到。ReadProcessMemory 不需要注入，
渲染进程拒绝 frida-agent 但仍允许 VM_READ，因此内存抓取是最稳的读取路径。

流程：点击服务号菜单触发授权 -> 等页面落地 -> 扫微信进程内存 ->
      提取全部 eyJvcGVuSWQ... 会话 -> 解码 -> 逐个 API 验证 -> 返回有效的。
"""

import base64
import ctypes
import json
import re
import subprocess
import sys
import time
from ctypes import wintypes

import wechat_oid

kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x06, 0x20, 0x40, 0x80}


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("__align1", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("__align2", wintypes.DWORD)]


PAT_B64 = re.compile(rb"eyJvcGVuSWQ[A-Za-z0-9+/=]{16,}")


def _wechat_pids():
    pids = []
    for image in ("Weixin.exe", "WeChatAppEx.exe"):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image],
                             capture_output=True)
        pids += [int(p) for p in re.findall(
            r"%s\s+(\d+)" % image.replace(".", r"\."),
            out.stdout.decode("gbk", errors="replace"))]
    return pids


def _scan_pid(pid, max_region=256 * 1024 * 1024, chunk=4 << 20):
    """返回该进程内存里所有 base64 session 串"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    found = []
    try:
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < 0x7FFFFFFF0000:
            if kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                       ctypes.sizeof(mbi)) == 0:
                break
            base, size, state, protect = (mbi.BaseAddress or 0, mbi.RegionSize,
                                          mbi.State, mbi.Protect)
            if (state == MEM_COMMIT and (protect & 0xFF) in PAGE_READABLE
                    and 0 < size <= max_region):
                off = 0
                while off < size:
                    n = min(chunk, size - off)
                    buf = (ctypes.c_char * n)()
                    read = ctypes.c_size_t(0)
                    if kernel32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n,
                                                  ctypes.byref(read)):
                        for m in PAT_B64.finditer(bytes(buf.raw[:read.value])):
                            found.append(m.group(0))
                    off += n
            addr = base + size
    finally:
        kernel32.CloseHandle(h)
    return found


def _decode_session(b64):
    """base64 -> openId。串可能自带 padding，先剥掉再按需补齐"""
    s = b64.decode("ascii") if isinstance(b64, (bytes, bytearray)) else b64
    s = s.rstrip("=")
    return json.loads(base64.b64decode(s + "=" * (-len(s) % 4)))["openId"]


def scan_openids(verify=True, verbose=False):
    """扫全部微信进程内存，返回 [(openid, 来源pid)]；verify=True 时只返验证通过的"""
    sessions = {}          # openid -> 首次出现的 pid
    for pid in _wechat_pids():
        for b64 in _scan_pid(pid):
            try:
                oid = _decode_session(b64)
            except Exception:
                continue
            if oid and oid not in sessions:
                sessions[oid] = pid
    if verbose:
        print("内存中发现 %d 个会话候选" % len(sessions))
    if not verify:
        return list(sessions.items())
    for oid, pid in sessions.items():
        code, body = wechat_oid.verify(oid)
        if code == 200:
            return [(oid, pid)]
        if verbose:
            print("  %s… -> HTTP %s" % (oid[:8], code))
    return []


if __name__ == "__main__":
    verbose = "-v" in sys.argv
    t0 = time.time()
    hits = scan_openids(verbose=verbose)
    if hits:
        oid, pid = hits[0]
        print("有效 openid: %s（来自 pid=%d，耗时 %.1fs）" % (oid, pid, time.time() - t0))
        sys.exit(0)
    print("未找到有效 openid（授权可能未触发，耗时 %.1fs）" % (time.time() - t0))
    sys.exit(1)

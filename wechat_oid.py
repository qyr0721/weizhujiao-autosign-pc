#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat_oid.py — 从 PC 微信（4.x / Radium CEF）Cookie 库解密提取微助教 openid

原理（与手机端读 XWeb Cookie 库同构）：
  1. 微信内置浏览器按 Chromium 规范把 v18.teachermate.cn 的 session Cookie
     以 v10(AES-256-GCM) 加密存在 .../radium/web/profiles/*/Network/Cookies
  2. 密钥在 .../radium/web/Local State 的 os_crypt.encrypted_key，
     由 Windows DPAPI 保护（同一用户可解）
  3. 解出明文即 base64({"openId":"<32位hex>"})，与手机端 Cookie 格式一致

用法：
  python wechat_oid.py            # 输出当前 openid 并验证是否有效
  python wechat_oid.py --quiet    # 只输出第一个有效的 openid（供其他脚本调用）

依赖：Python 3.8+ **纯标准库**（AES-GCM 走 Windows CNG 原生接口；
如装有 pycryptodome 则自动作备用）。仅读，不写任何文件。
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import ctypes
from ctypes import wintypes

WECHAT_DATA = os.path.expandvars(r"%APPDATA%\Tencent\xwechat")
PROFILES_DIR = os.path.join(WECHAT_DATA, "radium", "web", "profiles")
LOCAL_STATE = os.path.join(WECHAT_DATA, "radium", "web", "Local State")
API_SIGNS = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(enc: bytes) -> bytes:
    """CryptUnprotectData：同一 Windows 用户无需交互即可解 DPAPI  blob"""
    blob_in = DATA_BLOB(len(enc), ctypes.create_string_buffer(enc, len(enc)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData 失败（换了 Windows 用户就会失败）")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


class _BCRYPT_AUTH_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.c_void_p),
        ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.c_void_p),
        ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.c_void_p),
        ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.c_void_p),
        ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", ctypes.c_ulong),
    ]


def _aes_gcm_cng(key: bytes, nonce: bytes, ct: bytes, tag: bytes) -> bytes:
    """Windows CNG（bcrypt.dll）AES-256-GCM 解密，自带认证标签校验"""
    bcrypt = ctypes.windll.bcrypt
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    bcrypt.BCryptOpenAlgorithmProvider.restype = ctypes.c_long
    bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    bcrypt.BCryptSetProperty.restype = ctypes.c_long
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    bcrypt.BCryptGenerateSymmetricKey.restype = ctypes.c_long
    bcrypt.BCryptDecrypt.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
    bcrypt.BCryptDecrypt.restype = ctypes.c_long
    bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    alg = ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0) != 0:
        raise OSError("BCryptOpenAlgorithmProvider 失败")
    try:
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        if bcrypt.BCryptSetProperty(alg, "ChainingMode", mode, len(mode), 0) != 0:
            raise OSError("设置 GCM 链模式失败")
        kh = ctypes.c_void_p()
        if bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(kh), None, 0,
                                             key, len(key), 0) != 0:
            raise OSError("生成对称密钥句柄失败")
        try:
            nonce_buf = ctypes.create_string_buffer(bytes(nonce), len(nonce))
            tag_buf = ctypes.create_string_buffer(bytes(tag), len(tag))
            info = _BCRYPT_AUTH_MODE_INFO()
            info.cbSize = ctypes.sizeof(info)
            info.dwInfoVersion = 1
            info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
            info.cbNonce = len(nonce)
            info.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
            info.cbTag = len(tag)
            pt = ctypes.create_string_buffer(len(ct))
            done = ctypes.c_ulong(0)
            st = bcrypt.BCryptDecrypt(kh, ct, len(ct), ctypes.byref(info), None, 0,
                                      pt, len(pt), ctypes.byref(done), 0)
            if st != 0:
                raise OSError("GCM 认证标签校验失败 NTSTATUS=0x%08X" % st)
            return pt.raw[:done.value]
        finally:
            bcrypt.BCryptDestroyKey(kh)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def aes_gcm_decrypt(key: bytes, value: bytes) -> bytes:
    """Chromium v10 格式：b'v10' + nonce(12) + ciphertext + tag(16)"""
    if not value.startswith(b"v10"):
        raise ValueError("非 v10 加密值（可能版本变更）")
    nonce, ct, tag = value[3:15], value[15:-16], value[-16:]
    try:
        return _aes_gcm_cng(key, nonce, ct, tag)
    except Exception:
        from Crypto.Cipher import AES      # 备用：装了 pycryptodome 就走它
        return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)


def all_cookie_dbs():
    """所有 profile 的 Cookies 库，按修改时间倒序"""
    if not os.path.isdir(PROFILES_DIR):
        sys.exit("未找到 %s（微信未安装或版本不同）" % PROFILES_DIR)
    cands = []
    for name in os.listdir(PROFILES_DIR):
        p = os.path.join(PROFILES_DIR, name, "Network", "Cookies")
        if os.path.isfile(p):
            cands.append((os.path.getmtime(p), p))
    cands.sort(reverse=True)
    return cands


def _win_read_shared(path: str) -> bytes:
    """Windows API 带共享标志读文件（应对占用锁）"""
    GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 1, 2, 4
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    h = k.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                      None, OPEN_EXISTING, 0, None)
    if h == ctypes.c_void_p(-1).value or not h:
        raise OSError("CreateFileW 失败 err=%d" % ctypes.get_last_error())
    try:
        chunks, buf = [], ctypes.create_string_buffer(1 << 20)
        read = wintypes.DWORD(0)
        while True:
            if not k.ReadFile(h, buf, len(buf), ctypes.byref(read), None):
                break
            if read.value == 0:
                break
            chunks.append(buf.raw[:read.value])
        return b"".join(chunks)
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def readable_copy(path: str) -> str:
    """返回一个可 sqlite 打开的路径：优先原文件，被占用则复制到临时目录"""
    import tempfile
    try:
        con = sqlite3.connect("file:%s?immutable=1" % path.replace("?", "%3f").replace("#", "%23"),
                              uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        con.close()
        return path
    except Exception:
        pass
    try:
        import shutil
        tmp = os.path.join(tempfile.gettempdir(), "tm_cookie_%d.db" % os.getpid())
        shutil.copyfile(path, tmp)
        return tmp
    except Exception:
        pass
    tmp = os.path.join(tempfile.gettempdir(), "tm_cookie_%d.db" % os.getpid())
    with open(tmp, "wb") as f:
        f.write(_win_read_shared(path))
    return tmp


def get_openids():
    """返回 [(openid, db, mtime)]，覆盖所有 profile（一台电脑可能登录多个微信号）"""
    with open(LOCAL_STATE, "r", encoding="utf-8") as f:
        raw = base64.b64decode(json.load(f)["os_crypt"]["encrypted_key"])
    if raw.startswith(b"DPAPI"):        # Chromium 惯例：前缀 DPAPI + DPAPI blob
        raw = raw[5:]
    key = dpapi_decrypt(raw)
    out = []
    for mtime, db in all_cookie_dbs():
        try:
            src = readable_copy(db)
        except Exception:
            continue
        try:
            con = sqlite3.connect("file:%s?immutable=1" % src.replace("?", "%3f").replace("#", "%23"),
                                  uri=True)
            row = con.execute(
                "SELECT encrypted_value FROM cookies WHERE host_key='v18.teachermate.cn' "
                "AND name='session' ORDER BY expires_utc DESC LIMIT 1").fetchone()
            con.close()
        except Exception:
            continue
        if not row:
            continue
        try:
            session = aes_gcm_decrypt(key, row[0]).decode("utf-8", "replace")
            idx = session.find("eyJ")   # Radium 明文前有 32 字节附加头，eyJ = '{"'
            if idx >= 0:
                out.append((json.loads(base64.b64decode(session[idx:] + "=="))["openId"], db, mtime))
        except Exception:
            continue
    return out


def verify(oid: str):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(API_SIGNS, headers={"User-Agent": UA, "Referer": REF, "openId": oid})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status, r.read(120).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(120).decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def current_valid_openid():
    """遍历所有 profile，返回第一个 API 验证为 200 的 openid；没有则返回 (None, 原因)"""
    found = get_openids()
    if not found:
        return None, "所有 profile 都没有 teachermate session"
    last = None
    for oid, db, _ in found:
        code, body = verify(oid)
        if code == 200:
            return oid, db
        last = "HTTP %s %s" % (code, body[:60])
    return None, last or "未知"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="只输出第一个有效的 openid")
    args = ap.parse_args()

    found = get_openids()
    if not found:
        sys.exit("所有 profile 都没有 teachermate session——请先在微信内置浏览器里打开一次签到页")

    for oid, db, _ in found:
        code, body = verify(oid)
        alive = code == 200
        if args.quiet and alive:
            print(oid)
            break
        if not args.quiet:
            print("openid      : %s…%s  [%s]" % (
                oid[:8], oid[-8:],
                "有效" if alive else "已过期（需重新触发授权）" if code == 401 else "HTTP %s" % code))
            print("  来源       : %s" % db)
            print("  API 验证   : %s" % body[:60])
    else:
        if args.quiet and not any(verify(o)[0] == 200 for o, _, _ in found):
            pass

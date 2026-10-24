#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth_refresher.py — 取号调度器：驱动 PC 微信内置浏览器完成静默授权，取回新 openid

简化流程（用户负责把文件传输助手聊天窗口打开并放好，脚本只做三件事）：
  ① 按窗口标题找到【文件传输助手】聊天窗口（微信 4.x 支持聊天窗口独立拖出）
  ② 窗口里已有链接卡片？有 -> 跳过发送；没有 -> 图像定位输入框，粘贴链接回车
  ③ 图像定位并点击链接卡片（多张时点最下面=最新）-> 内置浏览器静默授权
  ④ 等 ~8 秒落地 -> 读 Cookie 取 openid 并验证 -> 关内置浏览器 -> 恢复焦点

模板来源：capture_templates.py（一次性录制两张：输入框、链接卡片）
依赖：numpy + opencv-python-headless（pip install numpy opencv-python-headless）
     其余全部标准库（窗口操作、键鼠输入走 ctypes）

安全设计：
  - 默认 --dry-run，只定位不动作；真实执行需显式 --go
  - 两次刷新之间有 min_refresh_gap 冷却
  - 每步定位都有超时，找不到就放弃本轮（不瞎点）
"""

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

import wechat_oid

try:
    import numpy as np
    import cv2
except ImportError:
    sys.exit("需要图像识别库：pip install numpy opencv-python-headless")

HERE = os.path.dirname(os.path.abspath(__file__))
OAUTH_URL = ("https://open.weixin.qq.com/connect/oauth2/authorize"
             "?appid=wxa153455f3ef1d9f9"
             "&redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin"
             "&response_type=code&scope=snsapi_base&state=&connect_redirect=1#wechat_redirect")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def _enable_dpi_aware():
    """设为按显示器 DPI 感知（V2），否则 GetWindowRect 拿到的是虚拟化坐标，
    与 BitBlt 截图的物理像素对不上——点击会飘、模板区域会错"""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True                                    # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # PER_MONITOR_AWARE
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()          # SYSTEM_AWARE
        return True
    except Exception:
        return False


DPI_AWARE = _enable_dpi_aware()


def _setup_apis():
    """给所有返回句柄/指针的 API 设显式原型——64 位下不设 restype 会把指针截断成 32 位"""
    u, k, g = user32, kernel32, gdi32
    u.GetDC.restype = ctypes.c_void_p
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.FindWindowW.restype = ctypes.c_void_p
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]
    g.CreateCompatibleDC.restype = ctypes.c_void_p
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.CreateCompatibleBitmap.restype = ctypes.c_void_p
    g.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    g.SelectObject.restype = ctypes.c_void_p
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                            ctypes.c_char_p, ctypes.c_void_p, wintypes.UINT]
    g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    g.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]


_setup_apis()

VK_RETURN, VK_CONTROL, VK_MENU, VK_V, VK_F4 = 0x0D, 0x11, 0x12, 0x56, 0x73
KEYEVENTF_KEYUP = 0x0002
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_UNICODE = 0x0004
WM_CLOSE = 0x0010
SRCCOPY = 0x00CC0020


# ============================================================ 键鼠输入（ctypes）

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inp):
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def click(x, y):
    """绝对坐标点击"""
    vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    nx = int(x * 65535 / max(vw - 1, 1))
    ny = int(y * 65535 / max(vh - 1, 1))
    for flags in (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                  MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        m = INPUT(type=INPUT_MOUSE)
        m.mi = MOUSEINPUT(nx, ny, 0, flags, 0, None)
        _send(m)
        time.sleep(0.03)


def key(vk):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def type_text(s):
    """Unicode 直输（中文无障碍）"""
    for ch in s:
        k = INPUT(type=INPUT_KEYBOARD)
        k.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None)
        _send(k)
        k = INPUT(type=INPUT_KEYBOARD)
        k.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
        _send(k)
        time.sleep(0.01)


def paste():
    """Ctrl+V：必须按住 Ctrl 再按 V（之前的写法 Ctrl 先松，V 单独打进去会触发输入法）"""
    user32.keybd_event(VK_CONTROL, 0, 0, 0)              # Ctrl 按住
    time.sleep(0.03)
    user32.keybd_event(VK_V, 0, 0, 0)                    # V 按下
    time.sleep(0.02)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)      # V 松开
    time.sleep(0.03)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)  # Ctrl 松开


def set_clipboard(text):
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    if not user32.OpenClipboard(None):
        raise RuntimeError("打开剪贴板失败")
    h = None
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise RuntimeError("GlobalAlloc 失败")
        p = kernel32.GlobalLock(h)
        if not p:
            raise RuntimeError("GlobalLock 失败")
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            h = None
            raise RuntimeError("SetClipboardData 失败")
        h = None        # 成功后系统接管这块内存
    finally:
        if h:
            kernel32.GlobalFree(h)
        user32.CloseClipboard()


# ============================================================ 窗口操作

def _proc_name(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def enum_windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls, 256)
            out.append((h, buf.value, cls.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def find_wechat(cfg):
    """找微信主窗口：按进程名 weixin.exe + 标题/类名过滤（兼容 4.x 的 Qt 窗口）。
    双账号时有多个窗口：优先完整在屏内的，再按 hwnd 排序保证确定性。"""
    want_title = cfg.get("wechat_window_title", "微信")
    want_cls = cfg.get("wechat_window_class", "")
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    cands = []
    for h, title, cls in enum_windows():
        if _proc_name(h) != "weixin.exe":
            continue
        if title == want_title or (want_cls and cls == want_cls) or want_title in title:
            r = window_rect(h)
            onscreen = r[0] >= 0 and r[1] >= 0 and r[2] <= vw and r[3] <= vh
            cands.append((0 if onscreen else 1, h, r))     # 在屏内的排前面
    if not cands:
        return None
    cands.sort()                                          # 先按在屏状态，再按 hwnd，确定性
    _, hwnd, r = cands[0]
    if len(cands) > 1:
        log("警告：找到 %d 个微信主窗口，选用 hwnd=%s 矩形=%s（多账号时用 config 的 "
            "wechat_window_title 指定）" % (len(cands), hwnd, r))
    return hwnd


def find_chat_window(cfg):
    """找用户打开的【文件传输助手】独立聊天窗口（微信 4.x 支持聊天窗口独立）。
    用户只需把这个窗口打开并放到屏幕角落，脚本按窗口标题定位。"""
    want = cfg.get("chat_window_title", "文件传输助手")
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    cands = []
    for h, title, cls in enum_windows():
        if _proc_name(h) != "weixin.exe":
            continue
        if want in title:
            r = window_rect(h)
            if r[0] <= -32000:            # 最小化
                cands.append((2, h, r))
                continue
            onscreen = r[0] >= 0 and r[1] >= 0 and r[2] <= vw and r[3] <= vh
            cands.append((0 if onscreen else 1, h, r))
    if not cands:
        return None
    cands.sort()
    if len(cands) > 1:
        log("警告：找到 %d 个标题含「%s」的窗口，选用 hwnd=%s" % (len(cands), want, cands[0][1]))
    return cands[0][1]


def clamp_rect(rect):
    """把矩形限制在虚拟屏内（窗口可能被拖出屏幕边缘）"""
    vl = user32.GetSystemMetrics(76)
    vt = user32.GetSystemMetrics(77)
    vr = vl + user32.GetSystemMetrics(78)
    vb = vt + user32.GetSystemMetrics(79)
    l, t, r, b = rect
    return (max(l, vl), max(t, vt), min(r, vr), min(b, vb))


def force_foreground(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(VK_MENU, 0, 0, 0)          # ALT 点按绕过前台锁
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)


def close_browser_windows(keywords, main_hwnd=None):
    """关掉微信内置浏览器窗口：只认 WeChatAppEx.exe 进程（绝不能按标题匹配——
    用户自己的窗口标题也可能含"微助教"等字样，会误关）"""
    closed = 0
    for h, title, cls in enum_windows():
        if h == main_hwnd:
            continue
        if _proc_name(h) == "wechatappex.exe":
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
            closed += 1
    return closed


def close_front_appex(main_hwnd):
    """兜底：若当前前台窗口是微信内置浏览器（WeChatAppEx 进程），发 Alt+F4"""
    fg = user32.GetForegroundWindow()
    if fg and fg != main_hwnd and _proc_name(fg) == "wechatappex.exe":
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_F4, 0, 0, 0)
        user32.keybd_event(VK_F4, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.5)
        return True
    return False


# ============================================================ 屏幕截图 + 模板匹配

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def grab(rect=None):
    """截取屏幕区域，返回 (BGR ndarray, 区域左上角屏幕坐标)。rect=(l,t,r,b)"""
    vl = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vt = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    if rect is None:
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        rect = (vl, vt, vl + vw, vt + vh)
    l, t, r, b = rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        raise ValueError("无效截图区域 %s" % (rect,))
    hdc = user32.GetDC(None)
    try:
        mem = gdi32.CreateCompatibleDC(hdc)
        try:
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            try:
                gdi32.SelectObject(mem, bmp)
                gdi32.BitBlt(mem, 0, 0, w, h, hdc, l, t, SRCCOPY)
                bi = BITMAPINFOHEADER()
                bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bi.biWidth, bi.biHeight = w, -h     # 负高度 = 自上而下
                bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
                buf = ctypes.create_string_buffer(w * h * 4)
                got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
                if got != h:
                    raise OSError("GetDIBits 只返回 %d/%d 行" % (got, h))
                img = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
                return img[:, :, :3].copy(), (l, t)     # BGRA -> BGR
            finally:
                gdi32.DeleteObject(bmp)
        finally:
            gdi32.DeleteDC(mem)
    finally:
        user32.ReleaseDC(None, hdc)


TEMPLATES = {                      # 模板名 -> 说明
    "input_box": "文件传输助手聊天窗口底部的消息输入框",
    "link_card": "聊天里发出去的链接卡片（微助教授权链接）",
    "menu_signin": "微助教服务号聊天窗口底部的【签到】菜单按钮",
}


def template_path(cfg, name):
    d = os.path.join(HERE, cfg.get("templates_dir", "templates"))
    return os.path.join(d, "tpl_%s.png" % name)


def locate(cfg, name, region=None, confidence=None):
    """在屏幕上找模板。region=(l,t,r,b) 限定搜索范围（通常传微信窗口矩形）。
    返回 (屏幕绝对x, 屏幕绝对y, 匹配置信度) 或 None"""
    path = template_path(cfg, name)
    if not os.path.isfile(path):
        raise FileNotFoundError("缺少模板 %s（%s）——请先跑 capture_templates.py" % (path, TEMPLATES.get(name, "")))
    conf = confidence or cfg.get("match_confidence", 0.8)
    shot, origin = grab(region)
    tpl = cv2.imread(path, cv2.IMREAD_COLOR)
    if tpl is None:
        raise ValueError("模板文件损坏: %s" % path)
    if shot.shape[0] < tpl.shape[0] or shot.shape[1] < tpl.shape[1]:
        return None
    res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    if maxv < conf:
        return None
    cx = origin[0] + maxloc[0] + tpl.shape[1] // 2
    cy = origin[1] + maxloc[1] + tpl.shape[0] // 2
    return cx, cy, maxv


def locate_all(cfg, name, region=None, confidence=None, limit=8):
    """找模板的所有出现位置（非极大值抑制）。返回 [(屏幕x, 屏幕y, 置信度), ...]"""
    path = template_path(cfg, name)
    if not os.path.isfile(path):
        raise FileNotFoundError("缺少模板 %s" % path)
    conf = confidence or cfg.get("match_confidence", 0.8)
    shot, origin = grab(region)
    tpl = cv2.imread(path, cv2.IMREAD_COLOR)
    if tpl is None:
        raise ValueError("模板文件损坏: %s" % path)
    if shot.shape[0] < tpl.shape[0] or shot.shape[1] < tpl.shape[1]:
        return []
    res = cv2.matchTemplate(shot, tpl, cv2.TM_CCOEFF_NORMED)
    th, tw = tpl.shape[:2]
    out = []
    work = res.copy()
    for _ in range(limit):
        _, maxv, _, maxloc = cv2.minMaxLoc(work)
        if maxv < conf:
            break
        out.append((origin[0] + maxloc[0] + tw // 2,
                    origin[1] + maxloc[1] + th // 2, maxv))
        x0, y0 = max(maxloc[0] - tw // 2, 0), max(maxloc[1] - th // 2, 0)
        x1, y1 = min(maxloc[0] + tw // 2, work.shape[1]), min(maxloc[1] + th // 2, work.shape[0])
        work[y0:y1, x0:x1] = -1.0
    return out


def locate_click(cfg, name, region=None, timeout=10, what=None, dry=False):
    """带超时重试的定位点击。dry=True 时只定位不点击。返回 True/False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hit = locate(cfg, name, region)
        except (FileNotFoundError, ValueError) as e:
            log("模板错误: %s" % e)
            return False
        if hit:
            cx, cy, v = hit
            if dry:
                log("定位【%s】成功 (%.2f) -> 目标 (%d,%d)，dry-run 不点击" % (name, v, cx, cy))
            else:
                log("定位【%s】成功 (%.2f) -> 点击 (%d,%d)" % (name, v, cx, cy))
                click(cx, cy)
            return True
        time.sleep(0.5)
    log("定位【%s】超时失败（%s）" % (name, what or TEMPLATES.get(name, "")))
    return False


# ============================================================ 主流程

def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def refresh(cfg, dry_run=False):
    """执行一次取号。返回 (ok, openid, message)

    简化流程（用户负责把文件传输助手窗口打开并放好）：
      ① 按窗口标题找到【文件传输助手】聊天窗口
      ② 窗口里已有链接卡片？有 -> 跳过发送；没有 -> 点输入框、粘贴、回车
      ③ 点击链接卡片（多张时点最下面=最新的一张）
      ④ 等静默授权落地 -> 读 Cookie 取 openid -> 关内置浏览器 -> 恢复焦点
    """
    def act(msg):
        log(("DRY " if dry_run else "") + msg)
        if dry_run:
            time.sleep(0.05)

    last_gap_file = os.path.join(HERE, ".last_refresh")
    gap = cfg.get("min_refresh_gap", 60)
    if os.path.isfile(last_gap_file):
        age = time.time() - os.path.getmtime(last_gap_file)
        if age < gap:
            return False, None, "冷却中（%ds/<%ds），跳过" % (int(age), gap)

    mode = cfg.get("trigger_mode", "link_card")
    if mode == "service_menu":
        need = ["menu_student", "menu_signin"]
    else:
        need = ["input_box", "link_card"]
    missing = [n for n in need if not os.path.isfile(template_path(cfg, n))]
    if missing:
        return False, None, "缺少模板: %s——请先跑 capture_templates.py" % ", ".join(missing)

    # ① 找到目标窗口
    if mode == "service_menu":
        # 服务号菜单模式：用户把开着【微助教服务号】聊天的微信主窗口放好
        win_title = cfg.get("wechat_window_title", "微信")
        hwnd = find_wechat(cfg)
    else:
        win_title = cfg.get("chat_window_title", "文件传输助手")
        hwnd = find_chat_window(cfg)
    if not hwnd:
        return False, None, ("未找到标题含「%s」的微信窗口——请先在微信里打开对应聊天窗口"
                             "并放到屏幕角落保持可见" % win_title)
    rect = clamp_rect(window_rect(hwnd))
    if rect[2] - rect[0] < 50 or rect[3] - rect[1] < 50:
        user32.SetForegroundWindow(user32.GetForegroundWindow())
        return False, None, "目标窗口矩形异常 %s（可能正在关闭或已被关闭）" % (rect,)
    act("目标窗口 %s 区域 %s" % (win_title, rect))

    prev = user32.GetForegroundWindow()
    if dry_run:
        act("将把目标窗口置前（dry-run 不动作），原前台 hwnd=%s" % prev)
    else:
        force_foreground(hwnd)

    if mode == "service_menu":
        # 服务号菜单触发：点 学生(S) -> 点 签到(C) -> 等授权 -> 内存提取 openid
        # （磁盘 Cookies 库被微信独占锁死，内存抓取是唯一可靠读取路径）
        if not locate_click(cfg, "menu_student", rect, timeout=cfg.get("locate_timeout", 10),
                            dry=dry_run):
            user32.SetForegroundWindow(prev)
            return False, None, "找不到学生菜单（确认微助教服务号聊天在前台）"
        time.sleep(1.2)
        # 子菜单可能因状态切换没展开，补点一次学生(S)
        if not locate(cfg, "menu_signin", rect):
            hit = locate(cfg, "menu_student", rect)
            if hit:
                if dry_run:
                    act("子菜单未展开，将补点一次学生(S)（dry-run 不点击）")
                else:
                    log("子菜单未展开，补点一次学生(S)")
                    click(hit[0], hit[1])
                time.sleep(1.2)
        if not locate_click(cfg, "menu_signin", rect, timeout=cfg.get("locate_timeout", 10),
                            dry=dry_run):
            user32.SetForegroundWindow(prev)
            return False, None, "找不到签到菜单项"
        act("已点签到菜单，等授权落地")
        if dry_run:
            act("DRY 模式：不扫描内存、不关窗、恢复焦点")
            user32.SetForegroundWindow(prev)
            return False, None, "dry-run"
        import mem_oid
        # 授权落地时间不稳（实测 8~30 秒），轮询扫描直到拿到有效 openid
        hits = []
        deadline = time.time() + cfg.get("mem_wait", 45)
        while time.time() < deadline:
            hits = mem_oid.scan_openids(verbose=True)
            if hits:
                break
            act("授权尚未落地，5 秒后重试（剩余 %ds）" % int(deadline - time.time()))
            time.sleep(5)
        n = close_browser_windows(cfg.get("browser_title_keywords", ["微助教", "teachermate"]), hwnd)
        if n == 0:
            n = 1 if close_front_appex(hwnd) else 0
        act("关闭内置浏览器窗口 %d 个" % n)
        user32.SetForegroundWindow(prev)
        with open(last_gap_file, "w") as f:
            f.write(str(time.time()))
        if hits:
            oid, pid = hits[0]
            return True, oid, "取号成功 %s…%s（内存提取，pid=%d）" % (oid[:8], oid[-8:], pid)
        return False, None, "内存中未找到有效 openid（授权可能未触发）"

    # ② 已有卡片？有就不发
    try:
        existing = locate(cfg, "link_card", rect)
    except (FileNotFoundError, ValueError) as e:
        user32.SetForegroundWindow(prev)
        return False, None, "模板错误: %s" % e
    if existing:
        act("聊天里已有链接卡片（%.2f），跳过发送" % existing[2])
    else:
        if not locate_click(cfg, "input_box", rect, timeout=cfg.get("locate_timeout", 10), dry=dry_run):
            user32.SetForegroundWindow(prev)
            return False, None, "找不到输入框（确认文件传输助手窗口在前台且完整可见）"
        time.sleep(0.3)
        if not dry_run:
            set_clipboard(OAUTH_URL)
            paste()
            time.sleep(0.4)
            key(VK_RETURN)
        act("链接已发送，等卡片渲染")
        time.sleep(cfg.get("card_wait", 3.0))
        # 卡片没渲染出来就再等等
        deadline = time.time() + cfg.get("locate_timeout", 10)
        while time.time() < deadline:
            try:
                if locate(cfg, "link_card", rect):
                    break
            except (FileNotFoundError, ValueError):
                break
            time.sleep(0.5)
        else:
            user32.SetForegroundWindow(prev)
            return False, None, "卡片未出现（发送可能失败）"

    # ③ 点卡片（多张点最下面=最新的）
    cards = []
    try:
        cards = locate_all(cfg, "link_card", rect)
    except (FileNotFoundError, ValueError) as e:
        user32.SetForegroundWindow(prev)
        return False, None, "模板错误: %s" % e
    if not cards:
        user32.SetForegroundWindow(prev)
        return False, None, "找不到链接卡片"
    cx, cy, cv = max(cards, key=lambda c: c[1])       # y 最大 = 最下面 = 最新
    if dry_run:
        act("定位链接卡片 %d 处，选最新一处 (%.2f) -> 目标 (%d,%d)，dry-run 不点击"
            % (len(cards), cv, cx, cy))
    else:
        log("定位链接卡片 %d 处，点最新一处 (%.2f) -> (%d,%d)" % (len(cards), cv, cx, cy))
        click(cx, cy)
    act("已点卡片，等授权落地")
    time.sleep(cfg.get("auth_wait", 8))

    if dry_run:
        act("DRY 模式：不读 Cookie、不关窗、恢复焦点")
        user32.SetForegroundWindow(prev)
        return False, None, "dry-run"

    # ④ 读 openid 并验证
    oid, why = wechat_oid.current_valid_openid()
    n = close_browser_windows(cfg.get("browser_title_keywords", ["微助教", "teachermate"]), hwnd)
    if n == 0:
        n = 1 if close_front_appex(hwnd) else 0
    act("关闭内置浏览器窗口 %d 个" % n)
    user32.SetForegroundWindow(prev)

    with open(last_gap_file, "w") as f:
        f.write(str(time.time()))

    if oid:
        return True, oid, "取号成功 %s…%s" % (oid[:8], oid[-8:])
    return False, None, "取号失败：%s" % why


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="只演示流程，不真正操作微信")
    ap.add_argument("--go", action="store_true", help="确认真实执行")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not args.go and not args.dry_run:
        print("默认不动作。--dry-run 演示流程 / --go 真实执行 / 录模板用 capture_templates.py")
        sys.exit(0)

    ok, oid, msg = refresh(cfg, dry_run=args.dry_run and not args.go)
    print(("成功：" if ok else "失败：") + msg)
    sys.exit(0 if ok else 1)

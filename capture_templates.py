#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capture_templates.py — 录制图像识别模板（一次性）

按提示把微信摆到对应状态，然后在全屏遮罩上**拖框选中目标控件**，
自动裁剪保存到 templates/tpl_<名称>.png，供 auth_refresher.py 匹配定位。

只需录两张模板：
  tpl_input_box  文件传输助手聊天窗口底部的消息输入框
  tpl_link_card  聊天里发出去的链接卡片（微助教授权链接）

用法：python capture_templates.py          # 全量重录
      python capture_templates.py input_box   # 只重录某一张

依赖：numpy + opencv-python-headless + tkinter（Python 安装包自带）。
"""

import os
import sys
import time

import auth_refresher as ar   # 复用截图/窗口函数

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")

GUIDES = {
    "input_box": [
        "先打开【文件传输助手】聊天窗口（可独立拖出），放到屏幕角落保持可见。",
        "在遮罩出现后，拖框选中底部的【消息输入框】。",
    ],
    "link_card": [
        "在文件传输助手里发送这条链接（右键粘贴或手动输入均可）：",
        ar.OAUTH_URL,
        "等聊天气泡里出现【链接卡片】（有标题的小卡片）后，拖框选中它。",
        "提示：卡片就是真实授权页打开时的入口，录它最稳。",
    ],
    "menu_student": [
        "先打开【微助教服务号】聊天窗口（在主窗口里打开即可），放到屏幕角落保持可见。",
        "拖框选中聊天窗口底部的【学生(S)】菜单按钮。",
    ],
    "menu_signin": [
        "先点一下【学生(S)】让子菜单展开（签到(C)/答题(Q)/讨论(D)/抢答(R)/全部(A)）。",
        "拖框选中子菜单里的【签到(C)】。",
    ],
}


def pick_region():
    """全屏遮罩拖框，返回屏幕绝对坐标 (l, t, r, b) 或 None"""
    import tkinter as tk

    vl = ar.user32.GetSystemMetrics(76)
    vt = ar.user32.GetSystemMetrics(77)
    vw = ar.user32.GetSystemMetrics(78)
    vh = ar.user32.GetSystemMetrics(79)

    result = {}

    root = tk.Tk()
    root.geometry("%dx%d+%d+%d" % (vw, vh, vl, vt))
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.28)
    root.configure(bg="black")
    root.bind("<Escape>", lambda e: root.destroy())

    canvas = tk.Canvas(root, cursor="crosshair", bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    tip = canvas.create_text(vw // 2, 40, text="拖框选中目标 · Esc 取消",
                             fill="white", font=("Microsoft YaHei", 16))

    def on_press(e):
        result["x0"], result["y0"] = e.x_root, e.y_root
        result["rect"] = canvas.create_rectangle(e.x_root - vl, e.y_root - vt,
                                                 e.x_root - vl, e.y_root - vt,
                                                 outline="#00B42D", width=2)

    def on_drag(e):
        if "rect" in result:
            canvas.coords(result["rect"], result["x0"] - vl, result["y0"] - vt,
                          e.x_root - vl, e.y_root - vt)

    def on_release(e):
        x0, y0 = result["x0"], result["y0"]
        x1, y1 = e.x_root, e.y_root
        result["sel"] = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.mainloop()
    return result.get("sel")


def capture_one(name, cfg):
    print("\n" + "=" * 60)
    print("录制模板：%s  （%s）" % (name, ar.TEMPLATES.get(name, "")))
    for line in GUIDES.get(name, []):
        print("  · " + line)
    input("  准备好后按回车，3 秒后截屏并弹出选择框...")
    time.sleep(3)

    shot, origin = ar.grab()          # 全虚拟屏
    full_path = os.path.join(TPL_DIR, "capture_%s_full.png" % name)
    os.makedirs(TPL_DIR, exist_ok=True)
    import cv2
    cv2.imwrite(full_path, shot)

    sel = pick_region()
    if not sel:
        print("  已取消，未保存。")
        return False
    l, t, r, b = sel
    crop = shot[t - origin[1]: b - origin[1], l - origin[0]: r - origin[0]]
    if crop.size == 0:
        print("  框选区域为空，未保存。")
        return False
    out = os.path.join(TPL_DIR, "tpl_%s.png" % name)
    cv2.imwrite(out, crop)
    print("  已保存 %s（%dx%d）" % (out, crop.shape[1], crop.shape[0]))

    # 立即自检：在当前屏幕上能否匹配到（置信度低于阈值时警告）
    try:
        hit = ar.locate(cfg, name)
        if hit:
            print("  自检通过：当前屏幕可匹配（置信度 %.2f，中心 %s）" % (hit[2], hit[:2]))
        else:
            print("  自检未通过：当前屏幕匹配不到（可能被遮挡或状态变了，属正常）")
    except Exception as e:
        print("  自检跳过: %s" % e)
    return True


if __name__ == "__main__":
    cfg = ar.load_config(os.path.join(HERE, "config.json"))
    names = sys.argv[1:] or list(ar.TEMPLATES.keys())
    for n in names:
        if n not in ar.TEMPLATES:
            print("未知模板 %s（可选：%s）" % (n, ", ".join(ar.TEMPLATES)))
            continue
        capture_one(n, cfg)
    print("\n全部完成。接下来：python auth_refresher.py --dry-run 演示 / --go 真实执行")

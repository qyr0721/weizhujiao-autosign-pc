#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openid 生命周期实测（零第三方依赖，Python 3.8+）

一次运行回答三个问题：
  Q1  一个有效 openid 的实际寿命（多久后 API 开始 401）
  Q2  仅凭签到页下发的 session Cookie，能否免授权换新 openid
      （若可以 → 纯 PC 免微信方案成立；若不可以 → 必须借道微信客户端）
  Q3  普通浏览器访问微信授权链接是否已被关门（预期复现：是）

用法：
  python experiment_openid.py <32位hex openid> [--interval 30] [--max-wait 1200]

openid 获取方式（一次性人工）：微信 → 微助教服务号 → 学生 → 签到，
在页面加载完成前点右上角 ... → 复制链接，提取 student/sign?openid= 后面的 32 位 hex。
"""

import argparse
import base64
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

API_SIGNS = "https://www.teachermate.com.cn/wechat-api/v1/class-attendance/student/active_signs"
SIGN_PAGE = "https://v18.teachermate.cn/wechat-pro-ssr/student/sign"
OAUTH_URL = (
    "https://open.weixin.qq.com/connect/oauth2/authorize"
    "?appid=wxa153455f3ef1d9f9"
    "&redirect_uri=https%3A%2F%2Fv18.teachermate.cn%2Fapi%2Fv1%2Fwechat%2Fr%3Fm%3Ds_signin"
    "&response_type=code&scope=snsapi_base&state=&connect_redirect=1"
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REF = "https://app.teachermate.com.cn/wechat-pro-ssr/student/sign"


def fetch(url, extra_headers=None, jar=None, timeout=15):
    """返回 (status, set_cookie_list, body, final_url)"""
    if jar is not None:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REF,
                                               **(extra_headers or {})})
    try:
        resp = opener.open(req, timeout=timeout)
        cookies = resp.headers.get_all("Set-Cookie") or []
        return resp.status, cookies, resp.read().decode("utf-8", "replace"), resp.url
    except urllib.error.HTTPError as e:
        cookies = e.headers.get_all("Set-Cookie") or []
        return e.code, cookies, e.read().decode("utf-8", "replace"), e.url
    except Exception as e:  # 网络层错误
        return -1, [], "NETERR: %s" % e, url


def decode_session(cookies):
    """从 Set-Cookie 列表里找 session=base64({"openId":...})，解出 openId"""
    for c in cookies:
        if c.lower().startswith("session="):
            raw = c.split("=", 1)[1].split(";", 1)[0]
            try:
                return json.loads(base64.b64decode(raw).decode("utf-8", "replace")).get("openId")
            except Exception:
                return None
    return None


def probe_signs(oid, jar=None):
    headers = {} if oid is None else {"openId": oid}
    code, _, body, _ = fetch(API_SIGNS, headers, jar)
    return code, body[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("openid", help="32 位 hex 的有效 openid")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒（默认 30）")
    ap.add_argument("--max-wait", type=int, default=1200, help="最长等待秒（默认 1200）")
    args = ap.parse_args()

    oid = args.openid.strip()
    if len(oid) != 32 or any(c not in "0123456789abcdefABCDEF" for c in oid):
        print("[!] openid 应为 32 位 hex，收到：%r" % oid)
        sys.exit(2)

    print("=" * 64)
    print("Q0 初始验证：openid 是否有效")
    code, body = probe_signs(oid)
    print("    active_signs -> HTTP %s  %s" % (code, body))
    if code != 200:
        print("[!] openid 已失效或复制有误，请重新从公众号菜单获取后重试。")
        sys.exit(1)

    print("=" * 64)
    print("Q1 openid 寿命实测（每 %ds 探一次，上限 %ds）" % (args.interval, args.max_wait))
    t0 = time.time()
    ttl = None
    while time.time() - t0 < args.max_wait:
        time.sleep(args.interval)
        code, body = probe_signs(oid)
        mark = "alive" if code == 200 else "DEAD"
        print("    t+%4ds  HTTP %s  %s  %s" % (int(time.time() - t0), code, mark, body[:80] if code != 200 else ""))
        if code != 200:
            ttl = int(time.time() - t0)
            break
    if ttl is None:
        print("    %ds 内未失效（比预期 15 分钟更长）。可加大 --max-wait 复测。" % args.max_wait)
    else:
        print("    => openid 实际寿命约 %d 秒（%.1f 分钟）" % (ttl, ttl / 60))

    print("=" * 64)
    print("Q2 免授权换新 openid 测试（ session Cookie 路线 ）")
    jar = http.cookiejar.CookieJar()

    code, cookies, body, final = fetch(SIGN_PAGE, jar=jar)  # 不带 openid 参数
    new_oid = decode_session(cookies)
    print("    a) 无参数访问签到页 -> HTTP %s，Set-Cookie %d 个，新 openid：%s"
          % (code, len(cookies), new_oid or "（无）"))
    if new_oid and new_oid != oid:
        code2, body2 = probe_signs(new_oid, jar)
        print("       用该新 openid 请求 active_signs -> HTTP %s %s" % (code2, body2[:80]))
        if code2 == 200:
            print("    => 惊人发现：免授权即可换新 openid，纯 PC 方案成立！")
            return
    elif new_oid == oid:
        print("       （Cookie 里的还是同一个旧 openid）")

    code, cookies, body, final = fetch(SIGN_PAGE + "?openid=" + oid, jar=jar)  # 带旧 openid
    new_oid = decode_session(cookies)
    print("    b) 带旧 openid 访问签到页 -> HTTP %s，Cookie 内 openid：%s"
          % (code, new_oid or "（未刷新）"))

    code, body = probe_signs(None, jar)  # 不带 openId 头，仅靠 Cookie
    print("    c) 仅带 Cookie（无 openId 头）请求 active_signs -> HTTP %s %s" % (code, body[:100]))
    if code == 200:
        print("    => 服务端接受 Cookie 鉴权！执行器可改用 Cookie 方式（更稳）")

    code, cookies, body, final = fetch(OAUTH_URL, jar=jar)
    stub = "请在微信客户端" in body or len(body) < 2000
    print("    d) 同浏览器上下文访问授权链接 -> HTTP %s，%s"
          % (code, "存根页（浏览器路线已关门）" if stub else "真实授权页，需人工检查"))
    print("    => 结论：免授权换新 openid %s" % ("【不】" if code != 200 else "可能") + "可行，需借道微信客户端")

    print("=" * 64)
    print("Q3 完成。汇总：TTL=%s；Cookie 路线=%s" % (
        ("%ds" % ttl) if ttl else "未知",
        "可行" if code == 200 else "不可行"))


if __name__ == "__main__":
    main()

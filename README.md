# 微助教电脑端（单设备）自动签到 · 使用说明

单设备方案：**PC 微信当授权引擎，本地脚本当执行引擎**。手机端模块的取号能力由
PC 微信内置浏览器承担，执行逻辑从 `tm_flow.sh` 一对一移植。

```
┌─ PC（单设备）──────────────────────────────────────────────┐
│  用户：打开【微助教服务号】聊天（主窗口里），放屏幕角落        │
│              ↓                                             │
│  auth_refresher.py   取号调度器                             │
│   ① 图像定位点【学生(S)】→ ② 点【签到(C)】触发静默授权        │
│   ③ ReadProcessMemory 扫微信进程内存提取 session             │
│        ↓ openid（全新有效）                                  │
│  signer.py           签到执行器（纯 HTTP 常驻）               │
│   5s 轮询 → 命中即提交 → 401 立即重取                        │
└──────────────────────────────────────────────────────────────┘
```

> 为什么用内存提取：授权页打开后，微信会把 Cookies **数据库**独占锁死
> （任何外部进程甚至微信自己重开都读不到），但 session 的明文会留在浏览器
> 进程内存里。ReadProcessMemory 不需要注入、不受渲染进程反注入影响，
> 是本机验证过的唯一可靠读取路径。

## 文件

| 文件 | 作用 |
|---|---|
| `signer.py` | 执行器 + 主循环。`--once` 单轮调试 / `--status` 看状态 / `--refresh` 立即取号 / 默认守护进程 |
| `auth_refresher.py` | 取号调度器：点服务号菜单 → 触发授权 → 内存提取 openid。`--dry-run` / `--go` |
| `mem_oid.py` | 内存提取模块：ReadProcessMemory 扫微信进程，提取并验证 openid |
| `capture_templates.py` | 模板录制工具（一次性）：录【学生(S)】和【签到(C)】两张图 |
| `wechat_oid.py` | Cookie 数据库读取模块（备用路径：数据库恰好没被锁时用） |
| `notifier.py` | 日志 + 蜂鸣 + Webhook 通知 |
| `config.json` | 配置（从 `config.example.json` 复制） |
| `state.json` | 运行状态（已签集合、上次取号时间等），自动生成 |
| `templates/` | 录制的模板图，录制后生成 |

## 安装（两步）

```powershell
winget install -e --id Python.Python.3.12
pip install numpy opencv-python-headless
```

装完**关掉再重开 PowerShell**（刷新 PATH），然后 `python --version` 验证。

> 如果 `python` 还是打开微软商店：设置 → 应用 → 高级应用设置 → 应用执行别名，关掉 python.exe / python3.exe 两个别名。
> 其余全部走 Windows 原生 API（截图、键鼠、内存读取），无其他第三方库。

## 使用方式（三步）

**第 1 步：打开微助教服务号聊天**（在微信主窗口的聊天列表里点开），把微信主窗口放屏幕角落保持可见。脚本只认这个窗口，不用管其他。

**第 2 步：录两张模板**（一次性）

```powershell
python capture_templates.py
```

| 模板 | 录制时的状态 |
|---|---|
| `tpl_menu_student` | 服务号聊天开着，框选底部的【学生(S)】菜单按钮 |
| `tpl_menu_signin` | 先点一下学生(S)展开子菜单，框选【签到(C)】 |

录制要点：**宁小勿大**，只包住按钮本身。

**第 3 步：取号 + 签到**

```powershell
python auth_refresher.py --go          # 取一次号（实测全程约 20 秒）
python signer.py                       # 守护进程（Ctrl+C 停止）
```

取号逻辑：置前微信窗口 → 图像定位点【学生(S)】→ 点子菜单【签到(C)】→ 内置浏览器
静默授权落到签到页 → 轮询扫描进程内存直到拿到有效 openid → 关浏览器窗口 → 恢复焦点。

开机自启 + 崩溃自拉起（推荐 NSSM）：

```powershell
nssm install WeizhujiaoSigner "pythonw.exe" "C:\path\to\signer.py"
nssm set WeizhujiaoSigner AppDirectory "C:\path\to"
nssm start WeizhujiaoSigner
```

## 配置要点（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `lat` / `lon` | 30 / 104 | 教室坐标，**仅 isGPS>0 的签到才提交**。请填本人实际所在 |
| `jitter_deg` | 0.00003 | 坐标随机抖动（约 ±3 米） |
| `interval` | 5 | 轮询间隔（秒） |
| `refresh_every` | 660 | 主动取号周期（秒）；401 会立即触发 |
| `class_hours` | [7, 23] | 只在该时段取号（不碰微信）；轮询 24h 不断 |
| `trigger_mode` | service_menu | `service_menu`=服务号菜单触发（推荐，已实测）；`link_card`=旧的文件传输助手发链接方案 |
| `wechat_window_title` | 微信 | 主窗口标题（多账号时可指定） |
| `mem_wait` | 45 | 点签到后等授权落地的最长时间（秒），期间每 5 秒扫一次内存 |
| `match_confidence` | 0.8 | 模板匹配阈值，匹配不稳就降到 0.7 |
| `notify_webhook` | 空 | 企业微信机器人 / Bark / ServerChan 地址，留空则只日志+蜂鸣 |
| `beep_on` | true | 签到/取号结果蜂鸣提示 |

## 设计要点

- **执行层 100% 复用手机端**：接口、请求头、报文、305 幂等、坐标抖动、401 续号策略全部一致；
- **修复手机端两个已知限制**：多课并行只签第一场 → 改为遍历 `active_signs` 全量提交；锁屏/安全锁 → PC 无此问题；
- **低干扰**：只在需要时唤醒微信（~20 秒/11 分钟，仅上课时段）、结束恢复原窗口；
- **失败分层**：网络指数退避 / 401 立即取号（15 秒防打转）/ 取号失败 15 秒重试 / 业务失败下轮自然重试；
- **为什么不 hook 微信**：WMPFDebugger 等 Frida 方案对微信 4.x 需要逐版本找内存偏移
  （本机 WMPF 25799 不在任何工具支持列表），且注入有封号风险。菜单点击 + 内存读取
  不走注入、不改微信，全部走官方客户端行为。

## 常见问题

| 现象 | 处理 |
|---|---|
| "未找到标题含「微信」的微信窗口" | 微信没开或最小化，打开主窗口即可 |
| "找不到学生菜单" | 确认微助教服务号聊天在前台；模板不准就重录 |
| "内存中未找到有效 openid" | 授权没触发：看微信是否弹了二维码、账号是否关注了服务号；`mem_wait` 调大到 60 |
| 取号成功但签到 401 | openid 15 分钟寿命，等下一轮自动重取即可 |
| 换了微信版本后失效 | 重录模板；窗口标题变了就改 `wechat_window_title` |
| 登录了两个微信号 | 内存提取会遍历所有账号的会话，自动选 API 验证通过的那个 |
| 想彻底静默 | `notify_webhook` 配好 + `beep_on: false` |

## 合规提醒

- 仅个人学习研究使用；自动签到可能违反学校考勤规定，后果自负；请只在本人确实在场时使用；
- 本方案不 hook、不注入微信进程，全部走官方客户端行为与官方接口；
- 服务端改版会使方案失效，接口挖掘方法见原项目 `技术复盘.md`（https://github.com/qyr0721/weizhujiao-autosign/blob/main/docs/技术复盘.md）。

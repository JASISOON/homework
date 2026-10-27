# 作业备忘 · 项目交接文档

- 项目：作业备忘（Homework Desk）
- 版本：1.1.0（含未发布的界面 / 托盘改动，版本号尚未提升）
- 文档更新：2026-10-27
- 形态：Windows 桌面应用（Python + pywebview/WebView2）
- 数据源：深圳大学优课（TronClass）`https://lms.szu.edu.cn/api/todos`
- 目标用户：深圳大学在校学生

> 本项目使用的是非官方接口。它只能读取 TronClass 的待办列表，**不会代替用户在 LMS 中提交作业**。
> 学校系统页面或接口结构调整后，需要按本文档的“故障排查”章节更新适配代码。

---

## 1. 项目概览

程序提供一个正常的桌面主界面和一个可置顶的小型悬浮窗：

1. 首次打开时，用户输入深大统一身份认证账号。
2. 后端登录 `authserver.szu.edu.cn`，再完成 Keycloak/TronClass 的 SSO 流程。
3. 请求 `/api/todos` 获取待办作业，转换为应用内统一数据结构。
4. 前端按截止时间排序，逾期置顶，并按紧迫程度着色。
5. 用户可勾选完成；完成记录仅保存在本机。
6. 登录凭据和登录 Cookie 用 Windows DPAPI 加密保存在本机。
7. 应用可以通过 Windows 启动文件夹在登录时自动运行。

### 主要能力

- 读取真实待办作业（名称、课程、截止时间、类型/备注）。
- 未完成过滤、截止时间升序、逾期置顶。
- 48 小时内红色“紧急”，7 天内橙色“临近”，7 天以上普通样式。
- 统计：未完成、紧急、临近、逾期。
- 手动刷新、最后更新时间、每 30 秒倒计时。
- 完整界面 / 置顶悬浮窗切换。
- 主界面和悬浮窗数据通过本地 API + BroadcastChannel 同步。
- 加密凭据自动登录；会话失效时自动使用保存的凭据重新登录。
- 安装程序、桌面/开始菜单快捷方式、开机自启动、卸载程序。

### 窗口形态（2026-10 改版）

- 主窗口和悬浮窗都改为**无边框窗口**，顶部由网页自绘标题栏，不再是系统白条。
- 标题栏高 38px：左侧应用图标 + 标题，右侧最小化 / 最大化 / 关闭。
- 保留系统原生的四边 / 四角缩放、Win11 圆角与描边；最大化贴合工作区，不遮挡任务栏。
- 侧边滚动条为“隐形浮现”样式：滚动时出现，停止约 1 秒淡出，鼠标移到窗口右侧重新出现并变墨绿。
- 关闭主窗口**不退出**，只收起到任务栏通知区域；托盘图标常驻，可重新打开、切换悬浮窗或真正退出。
- 再次启动 EXE 会唤回已收起的窗口，不会重复起进程。

---

## 2. 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                    HomeworkDesk.exe                     │
│                                                         │
│  主窗口 (WebView2)            悬浮窗 (WebView2)          │
│  homework-reminder.html       floating.html              │
│          │                         │                     │
│          └──── BroadcastChannel ───┘                     │
│          │                                               │
│          │ 本机 HTTP API（127.0.0.1 随机端口）             │
│          ▼                                               │
│  homework_server.py（本地服务）                          │
│  · 登录 / TronClass API / 本地完成状态                     │
│  · DPAPI 加密凭据和 Cookie                               │
│          │                                               │
│          ▼                                               │
│  authserver.szu.edu.cn / lms.szu.edu.cn                  │
│                                                          │
│  外壳层 desktop_app.py：无边框窗口 / 自绘标题栏 / 缩放 /   │
│  托盘图标 / 单实例与唤回                                  │
└─────────────────────────────────────────────────────────┘
```

### 为什么是“本地服务 + WebView2”

- 登录过程需要跨多个域（`authserver.szu.edu.cn` → `identity.szu.edu.cn` → `lms.szu.edu.cn`）维护 Cookie，
  浏览器页面本身无法安全地直接完成这套流程。
- Python 后端负责 Cookie、DPAPI、CAS 登录和同源代理，前端只访问本机 API，不需要处理跨域。
- 桌面窗口由 pywebview 创建，不经过外部浏览器，悬浮窗是独立的置顶原生窗口。

### 窗口外壳：无边框 + 托盘

相关代码全在 `desktop_app.py`，前端配合在 `homework-reminder.html` / `floating.html`。

**无边框窗口**

- `webview.create_window(..., frameless=True, easy_drag=False, shadow=True)`。
  `easy_drag` 必须为 `False`，否则整页按下都会拖拽，按钮会点不动。
- 拖拽只认 `.pywebview-drag-region`（标题栏左侧、空白拖拽区、悬浮窗品牌区）。
- 缩放没有系统边框，前端在四边/四角放透明热区，`mousedown` 时调用
  `window_resize(target, edge)`，Python 端用 `ReleaseCapture()` +
  `SendMessageW(WM_NCLBUTTONDOWN, HTLEFT/...)` 交给系统原生缩放循环。
  调用时必须左键仍按下（用 `GetAsyncKeyState(VK_LBUTTON)` 校验）。
- Win11 圆角/描边用 DWM：`DWMWA_WINDOW_CORNER_PREFERENCE=33`、
  `DWMWA_BORDER_COLOR=34`；最大化时改直角并去掉描边。
- 去掉系统标题栏后客户区会缩小 16×39（100% DPI），所以在窗口 `shown` 事件里
  用 `window.resize(MAIN_WIDTH, MAIN_HEIGHT)` 把尺寸补回设计值。
- 无边框窗口最大化默认会盖住任务栏，`fit_maximized_bounds()` 通过
  `native.MaximizedBounds = Screen.FromControl(native).WorkingArea` 限制到工作区。

**前端标题栏**

- `--titlebar-h`：浏览器里为 `0px`，`html.is-desktop-app` 时为 `38px`。
  页面顶部内边距、词条、滚动条轨道都用这个变量对齐。
- 标题栏按钮通过 `data-win-action="minimize|toggle-maximize|close"` 绑定，
  双击拖拽区也会切换最大化。
- 最大化图标靠 JS 每 1.5 秒轮询 `window_state('main')` 同步，**不要**从 UI 线程
  调 `window.evaluate_js`（会死锁）。
- 浏览器里打开这两个 HTML 时不会显示标题栏和缩放层，方便单独调样式。

**托盘常驻**

- `TrayController` 用 WinForms `NotifyIcon` + `ContextMenuStrip`：
  打开主界面 / 显示悬浮窗 / 退出作业备忘，双击图标等同“打开主界面”。
- NotifyIcon 必须在 WinForms UI 线程创建：`_on_window_shown` 里通过
  `native.Invoke(Action(...))` 完成；菜单/双击回调也跑在 UI 线程，窗口 API
  一律丢到后台线程执行（`_dispatch`），否则会卡死界面。
- 关闭窗口（标题栏 × 或 Alt+F4）→ `hide_to_tray()`：隐藏主窗和悬浮窗，
  进程、本地服务、登录态都保留，第一次会弹一次气泡提示。
- 退出：托盘菜单 → `DesktopApplication._quit_from_tray()` 先 `begin_shutdown()`
  再释放托盘图标、销毁窗口，`webview.start()` 返回后停服务并写退出日志。
- 单实例：`Local\HomeworkDeskDesktopApp` 互斥体判断已运行；第二个实例改为
  `SetEvent(Local\HomeworkDeskShowMain)`，已在运行的实例收到后显示主窗口，然后
  第二个实例直接退出（不再弹“已经在运行”）。
- `SystemEvents.SessionEnding` 里直接退出，避免关机/注销被“关闭即隐藏”拦住。

**隐形滚动条**

- 主页面隐藏 `html` 原生滚动条，用 `.scroll-rail` + `.scroll-thumb` 自绘；
  `window.scroll` 时重算位置并显示，1.1 秒后淡出；右侧 14px 热区
  `pointerenter` 可重新唤出，滑块悬停变墨绿并加宽。
- 悬浮窗对 `.f-list` 单独做了一套（`f-scroll-rail` / `f-scroll-thumb`），
  轨道位置按列表 `getBoundingClientRect()` 计算，并监听 `ResizeObserver`。
- 弹窗（登录框等）内部仍使用系统细滚动条，不受影响。
- 层级约定（改动前务必确认，否则会互相抢鼠标）：
  - `.win-resize-layer` 必须 `z-index: 90`，高于标题栏（80）和滚动条（65），
    否则顶边/底边热区会被它们挡住。
  - 右边缘缩放条宽 4px；滚动滑块 `right: 4px`（悬浮窗同样）向内让开这段距离，
    悬停时回到 3px。以后调滑块宽度/位置时保持这个内缩量。

### 关键文件

| 文件 | 责任 |
| --- | --- |
| `desktop_app.py` | 桌面入口；无边框主窗口和悬浮窗；标题栏桥接、缩放、DWM 圆角；托盘图标；单实例与唤回；退出清理 |
| `homework_server.py` | 本地 HTTP API、登录、DPAPI、TronClass 数据归一化、完成状态 |
| `homework-reminder.html` | 主界面 HTML/CSS/JS；自绘标题栏、拖拽/缩放热区、隐形滚动条 |
| `floating.html` | 悬浮窗 HTML/CSS/JS；自绘头部、关闭按钮、隐形滚动条 |
| `homework-desk.ico` | 窗口图标与托盘图标 |
| `HomeworkDesk.spec` | PyInstaller 打包配置 |
| `installer.iss` | Inno Setup 安装脚本 |
| `ChineseSimplified.isl` | Inno Setup 简体中文语言文件 |
| `build.ps1` | 一键重新打包 EXE 和安装程序 |
| `release/` | 可分发的安装程序和校验值 |

---

## 3. 数据模型与接口

### 3.1 应用内作业结构

服务端将 LMS 待办统一转换为：

```json
{
  "id": "240522",
  "name": "作业名称",
  "course": "课程名称",
  "dueDate": "2026-10-29T23:59+08:00",
  "completed": false,
  "notes": "",
  "type": "作业",
  "source": "深大优课"
}
```

前端还会补上运行时的 `meta`（level、remaining、label），不会持久化。

### 3.2 本地 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/status` | 登录状态、用户名、自动登录配置 |
| GET | `/api/homework?refresh=1` | 获取作业；`refresh=1` 绕过服务端短缓存 |
| POST | `/api/login` | 手动登录；支持图形验证码 |
| POST | `/api/logout` | 退出并清除登录信息 |
| POST | `/api/homework/{id}/complete` | 仅本机标记完成 |
| GET | `/floating.html` | 悬浮窗页面 |

所有接口只监听 `127.0.0.1`，并检查 Host/Origin。密码不会返回给前端。

### 3.2.1 桌面桥 API（`js_api`，仅桌面版可用）

前端通过 `window.pywebview.api.*` 调用，`window_state` / `window_control` /
`window_resize` 是自绘标题栏和缩放专用的新接口。

| 方法 | 参数 | 说明 |
| --- | --- | --- |
| `window_state` | `target` | 返回 `{ok, maximized}`，用于同步最大化/还原图标 |
| `window_control` | `target, action` | `minimize` / `toggle-maximize` / `close`；主窗 `close` = 收起到托盘 |
| `window_resize` | `target, edge` | 触发系统原生缩放；`edge` 为 `left/right/top/.../bottom-right` |
| `toggle_floating` | — | 显示/隐藏悬浮窗，返回 `{open}` |
| `get_floating_state` | — | 主界面轮询悬浮窗是否打开 |
| `show_main` / `show_main_and_login` | — | 从悬浮窗回到主界面 |
| `hide_floating` | — | 收起悬浮窗 |
| `open_external` | `url` | 只允许 http/https，用系统浏览器打开 |

> 这些方法都由 pywebview 在非 UI 线程调度；**不要**在 WinForms 事件或
> `native.Invoke` 回调里直接调用 `window.evaluate_js` / `window.show()` 等，
> 需要时先丢到 `threading.Thread`。

### 3.3 深大登录流程

1. 请求 `https://lms.szu.edu.cn/login?next=/user/index`。
2. 跟随跳转进入 `authserver.szu.edu.cn/authserver/login`。
3. 解析 `pwdFromId` 表单，读取 `pwdEncryptSalt`、`execution`、`service`。
4. 按学校页面 `encrypt.js` 的算法加密密码：
   - AES-CBC / PKCS7
   - key = `pwdEncryptSalt`
   - 明文 = 64 位随机前缀 + 密码
   - IV = 16 位随机字符
   - Base64 输出
   - Windows 上使用 CNG `bcrypt.dll` 实现，未依赖 PyCryptodome
5. POST 用户名、加密密码和隐藏字段，跟随 CAS/Keycloak 重定向。
6. 用 `/api/todos` 验证登录是否真正完成。
7. 成功后用 DPAPI 保存 Cookie；如勾选“记住登录”，再用 DPAPI 保存加密凭据。

### 3.4 TronClass 字段兼容

服务端支持 `end_time` / `due_at` / `deadline` 等截止时间字段别名，类型映射表位于
`homework_server.py` 的 `TYPE_LABELS`。如果学校字段名变化，优先修改
`normalize_todos()` 和字段取值处。

---

## 4. 安全模型

- 本地服务只绑定 `127.0.0.1` 随机端口，不对外开放。
- 凭据和 Cookie 使用 **Windows DPAPI**，作用域为当前 Windows 用户。
- 加密文件：
  - `%LOCALAPPDATA%\HomeworkDesk\data\credentials.dat`
  - `%LOCALAPPDATA%\HomeworkDesk\data\session.dat`
- 这些文件不可复制到其他 Windows 用户/电脑解密。
- 日志不记录密码、Cookie 或请求表单。
- 登录密码只会发送到 `authserver.szu.edu.cn`。
- 本地 API 的 POST 会检查 Origin，避免浏览器跨站调用。
- 应用不包含自动更新器，不会在后台下载或执行远程代码。
- 发布二进制未签名，Windows SmartScreen 可能提示“未知发布者”；正式分发建议做代码签名。

> 注意：GitHub 仓库中绝对不要提交 `credentials.dat`、`session.dat`、`prefs.json`、
> `state.json`、日志或 `%LOCALAPPDATA%\HomeworkDesk\data` 下的任何内容。`.gitignore` 已覆盖这些路径。

---

## 5. 构建与发布

### 5.1 环境

已验证环境：

- Windows x64
- Python 3.14（正式发布环境）
- WebView2 Runtime（Windows 11 通常已内置）
- Python 包：`requests`、`pywebview 6.2.1`、`pyinstaller 6.22.3`
- Inno Setup 6.7.3（仅打包安装程序需要）

> 2026-10-27 这次窗口改版是在没有系统 Python / Inno Setup 的机器上完成的：
> 用临时 venv（Python 3.12.14 + `pywebview 6.2.1` + `pyinstaller 6.22.3`）执行
> `HomeworkDesk.spec`，产出 `dist\HomeworkDesk`，再把整目录覆盖到
> `%LOCALAPPDATA%\Programs\HomeworkDesk`（保留了 `unins000.*`）。
> 因此当前安装目录里的 EXE 是 **Python 3.12 构建**；正式发版仍建议用 3.14 跑
> `build.ps1` 生成安装包。

### 5.1.1 本次改版涉及的构建注意

- `HomeworkDesk.spec` 无需改动；`datas` 已包含两个 HTML 和 `homework-desk.ico`。
- 托盘图标取自 `hs.ICON_PATH`（打包后位于 `_internal\homework-desk.ico`）。
- 覆盖安装目录前先结束 `HomeworkDesk.exe`，并备份原目录（本次备份在
  `%TEMP%\opencode\hd-install-backup`）。
- 若只替换 `HomeworkDesk.exe` 而不替换 `_internal`，会因 Python 版本不一致启动失败；
  必须整目录覆盖。

### 5.2 一键构建

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

输出：

- `dist\HomeworkDesk\HomeworkDesk.exe`：便携版程序目录
- `dist_installer\作业备忘-安装程序-1.1.0.exe`：安装程序

### 5.3 单独构建

```powershell
py -3.14 -m pip install requests pywebview pyinstaller
py -3.14 -m PyInstaller --noconfirm --clean HomeworkDesk.spec
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" .\installer.iss
```

### 5.4 版本升级

1. 修改 `homework_server.py` 的 `APP_VERSION`。
2. 修改 `installer.iss` 的 `MyAppVersion`、`OutputBaseFilename`、`VersionInfoVersion`。
3. 更新 `CHANGELOG.md`。
4. 重新运行 `build.ps1`。
5. 用新安装包覆盖安装；`%LOCALAPPDATA%\HomeworkDesk\data` 会保留。

### 5.5 GitHub 发布

安装包约 13.8 MB，小于 GitHub 单文件限制，可以直接放在 `release/`，也建议在 GitHub
Releases 中作为附件上传。发布前计算校验值：

```powershell
Get-FileHash .\release\作业备忘-安装程序-1.1.0.exe -Algorithm SHA256
```

---

## 6. 已验证场景

本机已实际测试：

- 真实深大账号首次登录成功，并同步到待办数据。
- 删除会话文件后重启服务，自动使用 DPAPI 加密凭据重新登录成功。
- 主界面加载真实数据、刷新成功。
- 主界面打开悬浮窗后，悬浮窗置顶显示，尺寸约 380×500。
- 悬浮窗点击“完整界面”回到主界面。
- 悬浮窗标题栏关闭按钮回到主界面，不退出程序。
- 关闭主窗口只把窗口收起到托盘，进程和本地服务继续在后台运行。
- 托盘图标可重新打开主界面、切换悬浮窗，并从托盘菜单真正退出。
- 重复双击快捷方式会唤回已收起的窗口，不会产生第二个实例。
- 安装程序创建桌面、开始菜单和 Startup 快捷方式，路径指向安装后的 EXE。
- 图形验证码前端流程（显示、重试）通过模拟接口验证。

窗口改版（源码预览 + 重新打包的 EXE）额外验证：

- 无边框窗口正常显示自绘 38px 标题栏（图标、标题、三个窗口按钮），无系统白条。
- 客户区尺寸补回 1180×830 / 380×500，标题栏不遮挡页面内容。
- 四边、四角缩放走系统原生循环；最大化贴合工作区、不盖任务栏，还原后恢复圆角。
- 主页面与悬浮窗滚动条均为“滚动浮现、约 1 秒淡出、右侧热区唤出、悬停墨绿”。
- 托盘图标创建成功；隐藏后进程、本地服务、自动登录状态都保留。
- 托盘“退出作业备忘”能在后台线程正常结束 `webview.start()` 并退出进程
  （用独立测试脚本调用 `_quit_from_tray()` 验证）。
- 打包后的 `HomeworkDesk.exe` 同样通过上述关闭→托盘→唤回→退出流程。

未使用真实账号验证的情形：

- 滑块验证码（当前不能自动完成）。
- 短信/FIDO/二次确认。
- 学校系统升级后的页面变化。

---

## 7. 已知限制与故障排查

### 7.1 滑块验证码

如果 `checkNeedCaptcha` 返回需要验证码，且学校页面 `captchaSwitch=2`，当前实现会提示
“稍后重试”。滑块验证无法用简单 HTTP 请求自动完成，这是当前最大的功能限制。

可选后续方案：

- 使用 WebView2 打开学校登录页，用户手动完成滑块，再由桌面壳读取同一 WebView2 的 Cookie；
- 或接入学校提供的官方 API / 授权方式。

### 7.2 自动登录失败

排查顺序：

1. 查看 `%LOCALAPPDATA%\HomeworkDesk\data\homework-desk.log`。
2. 确认密码最近没有修改。
3. 确认账号没有被要求短信/FIDO/二次确认。
4. 在应用里点击右上角账号按钮重新登录。
5. 删除 `credentials.dat` / `session.dat` 后重新登录（会丢失保存的登录状态）。

### 7.3 打开后空白或闪退

- 确认 WebView2 Runtime 已安装。
- 在应用安装目录尝试运行 `HomeworkDesk.exe`，检查是否被杀软拦截。
- 查看日志；日志在 `%LOCALAPPDATA%\HomeworkDesk\data`。
- 程序是单实例的：再次运行会唤回已有窗口，而不是启动第二个进程；
  如果窗口没出现，看托盘图标是否被收在“隐藏的图标”里。

### 7.3.1 关闭窗口后“找不到程序”

这是预期行为：关闭只是收起到托盘。查看任务栏右下角的“^ / 隐藏的图标”，
找到作业备忘图标双击即可；也可以在“设置 → 个性化 → 任务栏”里把该图标设为常显。
要真正结束进程：托盘右键 →「退出作业备忘」，或任务管理器结束 `HomeworkDesk.exe`。

### 7.3.2 托盘没有图标

- 先确认进程还在（任务管理器里能看到 `HomeworkDesk.exe`）。
- 日志里应有 `托盘图标已创建`；若为 `创建托盘图标失败`，把日志和 Windows 版本一起反馈。
- 图标可能只是被 Win11 收进了溢出区；拖出来即可常显。

### 7.4 自动启动

安装程序默认在以下目录创建快捷方式：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\作业备忘.lnk
```

不需要自动启动时删除该快捷方式即可。重新安装时可以取消“开机自动启动”任务。

### 7.5 数据位置

| 内容 | 路径 |
| --- | --- |
| 程序 | `%LOCALAPPDATA%\Programs\HomeworkDesk` |
| 数据/凭据 | `%LOCALAPPDATA%\HomeworkDesk\data` |
| 日志 | `%LOCALAPPDATA%\HomeworkDesk\data\homework-desk.log` |

---

## 8. 维护建议

- 学校登录页改版时，优先检查 `_load_login_page`、`PasswordFormParser`、
  `encrypt_szu_password` 和 `_post_login`。
- TronClass 字段变化时，检查 `normalize_todos()` 和前端 `FIELD_ALIASES`。
- 增加类型判断时，只改 `TYPE_LABELS`。
- 修改前端后，同步检查主界面和悬浮窗的数据刷新链路：
  `loadAssignments` → `BroadcastChannel('homework-desk')` → 对端刷新。
- 修改窗口行为时检查：
  - 主窗口关闭是否只收起到托盘，进程是否仍在运行；
  - 托盘菜单能否重新打开窗口并真正退出；
  - 悬浮窗关闭是否只回主界面；
  - 单实例互斥是否仍生效，重复启动能否唤回窗口。
- 不要在前端或仓库里写死账号、密码、Cookie、真实作业数据。

---

## 9. 交接清单

- [x] 主程序和悬浮窗源码
- [x] 真实账号登录与数据同步
- [x] DPAPI 本地凭据加密
- [x] 自动登录验证
- [x] 安装程序、快捷方式、自启动
- [x] 构建脚本和打包配置
- [x] 无边框标题栏、隐形滚动条、托盘常驻（2026-10 改版）
- [x] 使用文档、更新日志、交接文档
- [x] 安装包和 SHA256 校验值
- [ ] 开源许可证（当前未指定，公开发布前需决定）
- [ ] 代码签名（可选，正式分发建议）

---

## 10. 对外发布前检查

```text
1. 仓库内没有 .log、.dat、data、dist、build、dist_installer。
2. release/ 中的安装程序是最终构建产物，SHA256SUMS.txt 已更新。
3. README 和 HANDOVER 中没有个人路径、用户名、真实作业数据。
4. 安装包的版本号、文件名和 CHANGELOG 一致。
5. 用一台没有登录过本应用的 Windows 账户做一次冷启动测试。
```

> 2026-10-27 已把 `build/`、`dist/`、`__pycache__/` 移到项目目录**同级**的
> `_not-for-github/`（内含说明 README）。用网页拖拽上传时，不要把这个文件夹
> 一起拖进去；用 `git` 上传时它们本来就被 `.gitignore` 忽略。

## 11. 上传到 GitHub

当前项目目录已经按仓库结构整理，可以直接上传。推荐仓库名为 `homework-desk`。

上传前确认：目录里只有源码、文档、图标、预览图和 `release/`；
`build/`、`dist/`、`__pycache__/` 已移到项目目录外，登录数据在
`%LOCALAPPDATA%\HomeworkDesk\data`，两者都不在仓库里。

命令行方式（在项目目录下执行）：

```powershell
git init
git add .
git commit -m "Initial commit: Homework Desk 1.1.0"
git branch -M main
git remote add origin https://github.com/<你的用户名>/homework-desk.git
git push -u origin main
```

网页上传方式：

1. 在 GitHub 新建空仓库 `homework-desk`。
2. 把项目目录内的文件拖入上传页面。
3. 确认 `.gitignore` 也上传。
4. 安装包可以随 `release/` 一起提交，也可以在 GitHub Releases 中单独上传。

上传后建议在 GitHub Releases 中发布 `1.1.0`，附件使用
`release/作业备忘-安装程序-1.1.0.exe`，并附上 `release/SHA256SUMS.txt` 的内容。

当前仓库**没有附带开源许可证**。如果准备公开源码，发布前需要明确选择
MIT / Apache-2.0 / 私有仓库等授权方式；如仅自用，保持私有仓库即可。


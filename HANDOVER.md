# 作业备忘 · 项目交接文档

- 项目：作业备忘（Homework Desk）
- 版本：1.4.3（2026-10-29：作业内容 / 附件原样下载（第 3 章），系统要求变硬门槛（2.x「运行环境门槛」））
- 文档更新：2026-10-28
- 形态：Windows 桌面应用（Python + pywebview/WebView2）
- 数据源：深圳大学优课（TronClass）`https://lms.szu.edu.cn/api/todos`
- 目标用户：深圳大学在校学生

> 本项目使用的是非官方接口。它只能读取 TronClass 的待办列表，**不会代替用户在 LMS 中提交作业**。
> 学校系统页面或接口结构调整后，需要按本文档的“故障排查”章节更新适配代码。

---

## 1. 项目概览

程序提供一个正常的桌面主界面和一个 80×80 的桌面悬浮球：

1. 首次打开时，用户输入深大统一身份认证账号。
2. 后端登录 `authserver.szu.edu.cn`，再完成 Keycloak/TronClass 的 SSO 流程。
3. 请求 `/api/todos` 获取待办作业，转换为应用内统一数据结构。
4. 前端按截止时间排序，逾期置顶，并按紧迫程度着色。
5. 用户可勾选完成；完成记录仅保存在本机。
6. 登录凭据和登录 Cookie 用 Windows DPAPI 加密保存在本机。
7. 应用可以通过 Windows 启动文件夹在登录时自动运行。
8. 关闭主界面不退出程序：主窗隐藏、悬浮球出现在桌面，可拖拽、可点回主界面。

### 主要能力

- 读取真实待办作业（名称、课程、截止时间、类型/备注）。
- 未完成过滤、截止时间升序、逾期置顶。
- 48 小时内红色“紧急”，7 天内橙色“临近”，7 天以上普通样式。
- 统计：未完成、紧急、临近、逾期。
- 手动刷新、最后更新时间、每 30 秒倒计时。
- 主界面 ⇄ 悬浮球切换：关闭主界面即显示悬浮球，单击悬浮球回到主界面。
- 悬浮球带未完成作业数字角标，逾期/紧急时变红并呼吸；可拖拽并把位置记在本机。
- 主界面和悬浮球数据通过本地 API + BroadcastChannel 同步。
- 加密凭据自动登录；会话失效时自动使用保存的凭据重新登录。
- 外观主题：内置暖色 / 亮色 / 深色三套，可导入自定义主题，悬浮球同步换色。
- 安装程序、桌面/开始菜单快捷方式、开机自启动、卸载程序。

### 窗口形态（2026-10 改版）

- 主窗口改为**无边框窗口**，顶部由网页自绘标题栏，不再是系统白条。
- 标题栏高 38px：左侧应用图标 + 标题，右侧最小化 / 最大化 / 关闭。
- 保留系统原生的四边 / 四角缩放、Win11 圆角与描边；最大化贴合工作区，不遮挡任务栏。
- 侧边滚动条为“隐形浮现”样式：滚动时出现，停止约 1 秒淡出，鼠标移到窗口右侧重新出现并变墨绿。
- 关闭主窗口**不退出**：主窗隐藏、桌面出现 80×80 透明悬浮球（置顶、不进任务栏、不抢焦点），
  托盘图标同时常驻，三个入口都能重新打开主界面。
- 再次启动 EXE 会唤回已收起的窗口，不会重复起进程。
- 1.1.0 那个 380×500 的悬浮窗（`floating.html`）已被悬浮球取代：程序不再加载、也不打包它。

---

## 2. 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                    HomeworkDesk.exe                     │
│                                                         │
│  主窗口 (WebView2)          悬浮球 (原生分层窗口)         │
│  homework-reminder.html     ball_window.py · GDI+ 自绘    │
│          │                         │                     │
│          │  同一进程，直接调用 Python 对象                │
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
- 主窗口由 pywebview 创建，不经过外部浏览器；悬浮球是**原生分层窗口**（不走 WebView2，见 3.2 悬浮球窗口）。

### 窗口外壳：无边框 + 托盘

相关代码在 `desktop_app.py`（窗口外壳、托盘），主界面前端在 `homework-reminder.html`，悬浮球在 `ball_window.py`。

**无边框窗口**

- `webview.create_window(..., frameless=True, easy_drag=False, shadow=True)`。
  `easy_drag` 必须为 `False`，否则整页按下都会拖拽，按钮会点不动。
- 拖拽只认 `.pywebview-drag-region`（标题栏左侧、空白拖拽区、悬浮窗品牌区）。
- 缩放没有系统边框，前端在四边/四角放透明热区，`mousedown` 时调用
  `window_resize(target, edge)`。**不要**再用 `ReleaseCapture()` +
  `SendMessageW(WM_NCLBUTTONDOWN, HTLEFT/...)`：frameless 窗口上 DefWindowProc
  不会启动缩放循环（实测窗口完全不动），`ReleaseCapture()` 也只对本线程的鼠标捕获
  有效，而拖动时捕获在 WebView2 的 UI 线程手里。现在改成和拖动一样「自己跟」：
  记下起点光标与窗口矩形，按位移重算四条边（不小于 `WINDOW_MIN_*`）后
  `SetWindowPos`，松手由 `GetAsyncKeyState(VK_LBUTTON)` 判定。
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

**悬浮球窗口（原生分层窗口，`ball_window.py`）**

悬浮球**不是** WebView2 窗口。原因是 WebView2 在纯 HWND 宿主模式下没有逐像素透明：
`transparent=True`、`TransparencyKey` 色键、给宿主窗口加 `WS_EX_LAYERED` 都试过，
球外面始终会有一块方形底色（页面背景改洋红，整块方块就变洋红，证明确实是 WebView2 在画底）。
所以改用原生分层窗口自己画：

```python
CreateWindowExW(
    WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
    class_name, "", WS_POPUP, x, y, 80, 80, ...)
```

- 32bpp DIB + GDI+ 画圆球、阴影、文档图标、数字角标；
  `Bitmap(w, h, stride, PixelFormat.Format32bppPArgb, bits_ptr)` 直接绑定 DIB 内存
  （必须是 `PArgb`，用 `Argb` 会让 `AC_SRC_ALPHA` 的预乘算错，边缘出现黑边）。
- `UpdateLayeredWindow(..., ULW_ALPHA)` 提交位图，圆外 alpha=0 才是真透明。
- `WS_EX_NOACTIVATE` + `WM_MOUSEACTIVATE` 返回 `MA_NOACTIVATE`：点击球不抢前台焦点，
  全屏游戏/视频不会被打断。
- `WS_EX_TOOLWINDOW`：不进任务栏，也不出现在 Alt+Tab。
- 分层窗口**不响应 `WM_PAINT` 绘制**，所以状态一变就调 `_render()` 重新
  `UpdateLayeredWindow`；`WM_TIMER` 只在有紧急作业时重绘呼吸环。

交互（都在 `_wndproc` 里）：

| 消息 | 行为 |
| --- | --- |
| `WM_LBUTTONDOWN` | `_begin_drag()`：`SetCapture`，记下鼠标起点和窗口原点 |
| `WM_MOUSEMOVE` | 位移超过 4px 才算拖拽；用**绝对位移**重算 `origin + delta`，不是累加增量 |
| `WM_LBUTTONUP` | 拖过 → `move_to(..., notify=True)` 写 `ball-state.json`；没拖过 → 当单击，回主界面 |
| `WM_RBUTTONUP` | 收起悬浮球 |
| `WM_MOUSELEAVE` | `TrackMouseEvent(TME_LEAVE)` 配合，鼠标移出恢复常态 |

位置由 `clamp_ball_position()` 夹回当前显示器工作区，越界/拔掉显示器时不会跑到看不见的地方。

数据来源：`DesktopApplication._ball_snapshot()` 返回快照 dict
（`total` / `urgent` / `overdue` / `today` / `items[]`），`poll_forever()` 每 30 秒取一次
（取不到就画黄色 `!`），不阻塞主 UI 线程。`items` 是最近到期的 3 条，供展开卡片使用。

**弹层：右键菜单与概览卡片（2026-10 新增）**

球、菜单、卡片是**同一个分层窗口**，只是尺寸和画法不同 —— 这样不用维护第二个窗口、
第二套消息循环和位置记忆。三种形态共用一套展开动画与收起逻辑：

- 状态：`_popup_kind` 取 `""`（球） / `"card"` / `"menu"`；`_pop_rect` 是展开后的目标矩形，
  `_win_rect` 是当前实际窗口矩形。
- 动画：`_start_animation()` 记 `_win_rect → _pop_rect`，`AnimClock` 每 16 ms 发
  `WM_BALL_TICK`，窗口线程按缓动曲线插值后 `SetWindowPos`
  （相当于 Electron 的 `win.setBounds` 逐帧动画）。展开 easeOutCubic / 收起加速吸入。
  文字在 40% 之后才开始淡入（`_content_alpha`），避免小尺寸下文字挤压变形。
- **为什么不用 `SetTimer`**：`WM_TIMER` 被系统时钟节拍（默认 15.625 ms）量化，
  请求 16 ms 会被顶到两个节拍 ≈ 31 ms，动画只有 32 fps 且忽快忽慢；`timeBeginPeriod(1)`
  在 Win10+ 压不下来（实测仍 30.4 ms）。`AnimClock` 改用
  `CreateWaitableTimerExW(CREATE_WAITABLE_TIMER_HIGH_RESOLUTION)` + 一个等待线程，
  每拍 `PostMessage(WM_BALL_TICK)` 回窗口线程渲染；实测帧间隔中位 16.2 ms、
  最大 16.7 ms、没有一帧超过 20 ms。创建失败时自动退回 `SetTimer(10)`
  （10 ms 小于一个节拍，同样每拍都能触发）。
- 动画期间 `WM_PAINT` 被忽略：改窗口尺寸会招来 `WM_PAINT`，而定时器本来每帧都在重画，
  再画一次等于白费一半渲染时间。
- 毛玻璃：`_capture_backdrop()` 先 `Graphics.CopyFromScreen` 拍下卡片将覆盖的桌面，
  把球所在矩形涂成周边平均色（否则虚化后留一团绿影），再按
  `原图 → 1/10 → 1/4 → 原尺寸` 三级 bicubic 重采样模拟高斯模糊，最后叠一层
  深色半透明（卡片 ~190/255、菜单 ~198/255）圆角矩形 + 1px 亮描边。
- 外层画法：`_paint_card()` / `_paint_menu()` 都按「卡片坐标系」布局，再用
  `_content_offset()` 把卡片原点平移到窗口坐标，所以动画中途看到的总是卡片的正确裁剪区域。
- 命中：`_popup_click()` 按 `_popup_kind` 分流；卡片里是「× 按钮 / 作业行」，
  菜单里是 `_menu_index_at()` 的行索引（分隔线不命中）。
- 收起：展开完成后开 `WM_TIMER(TIMER_WATCH)`，每 40 ms 轮询 `GetAsyncKeyState`
  的左/右键，出现「按下」且指针在窗口外就收起；Esc 同样收起。
- 菜单项由 `DesktopApplication._ball_menu()` 现算，每项是
  `{"action", "label", "icon", "danger", "bold"}` 或 `{"sep": True}`；
  动作由 `_ball_menu_action(action)` 分发（`open` / `refresh` / `hide` / `quit`）。
- **线程约定**：窗口状态只允许在窗口线程改。外部线程的 `show()` / `hide()` 一律
  `PostMessage(WM_BALL_CMD)`、数据更新 `PostMessage(WM_BALL_DATA)`，
  否则会和动画帧抢同一块 DIB（GDI+ 并发画同一个 Bitmap 会花屏或崩）。

> 拖拽用绝对位移而不是增量，是因为旧版（WebView2 + IPC 增量）在 125% DPI 下每帧取整会累积
> 偏移，拖久了明显跟不上手；绝对位移每次从原点点算，没有漂移。
> 旧的 `ball.html` 和 `ball_move()` / `ball_position()` 桥接方法保留着但**已无前端调用**，
> 只作历史参考。

**托盘常驻**

- `TrayController` 用 WinForms `NotifyIcon` 常驻；左键单击直接打开主界面，右键弹出自绘菜单。
- 菜单是 `TrayMenuPopup`：**无边框 `Form` + GDI+ 手绘**，不是 `ContextMenuStrip`。
  原因：本环境下 pythonnet **不会回调被重写的虚拟方法**（`OnPaint`、`Timer.OnTick`、
  `ToolStripProfessionalRenderer.OnRender*` 都实测无效），只能靠 `+=` 事件委托，
  所以 ToolStrip 换肤这条路走不通。
- 菜单结构（`rows`）：
  - `("sep",)`：分隔线
  - `("item", 文本, 图标名, 危险色, 加粗, 回调)`：图标名取 `open` / `float` / `quit`
- 视觉常量：`TRAY_MENU_WIDTH=218`、`TRAY_MENU_ITEM_H=38`、`TRAY_MENU_HEADER_H=28`、
  `TRAY_MENU_ICON=18`，颜色集中在 `TRAY_COLORS`（浅底 `#FBFCF9`、悬停 `#E8F1EB`、
  主色 `#2F5B4C`、危险 `#B2423C`）。像素按 `DeviceDpi/96` 缩放，字号保持 pt。
- 交互：鼠标移动改 `_hover` 后 `Invalidate`；`Deactivate` 有 250ms 宽限期（弹出瞬间
  还没拿到焦点时不关闭），Esc 关闭；点击项先 `close()` 再执行回调。
- 「显示悬浮窗 / 收起悬浮窗」文案由 `TrayController._menu_rows()` 现算，
  依据是 `DesktopBridge.floating_visible`（构造函数传入 `float_state`）。
- Win11 圆角走 `DwmSetWindowAttribute(..., 33, DWMWCP_ROUND=2)`，并加
  `WS_EX_TOOLWINDOW` 让它不出现在 Alt+Tab。
- NotifyIcon 必须在 WinForms UI 线程创建：`_on_window_shown` 里通过
  `native.Invoke(Action(...))` 完成；菜单回调也跑在 UI 线程，窗口 API
  一律丢到后台线程执行（`_dispatch`），否则会卡死界面。
- 关闭窗口（标题栏 × 或 Alt+F4）→ `hide_to_tray()`：隐藏主窗、显示悬浮球，
  进程、本地服务、登录态都保留，第一次会弹一次气泡提示。
- 退出：托盘菜单 → `DesktopApplication._quit_from_tray()` 先 `begin_shutdown()`
  再释放托盘图标、销毁窗口，`webview.start()` 返回后停服务并写退出日志。
- 单实例：`Local\HomeworkDeskDesktopApp` 互斥体判断已运行；第二个实例改为
  `SetEvent(Local\HomeworkDeskShowMain)`，已在运行的实例收到后显示主窗口，然后
  第二个实例直接退出（不再弹“已经在运行”）。两个名字都支持
  `HOMEWORK_DESK_MUTEX_SUFFIX` 后缀，源码实例设一个后缀就能与安装版并存。
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

### 外观主题

主题不是「换一套 CSS」，而是**只覆盖 CSS 自定义属性**。主界面样式表里所有颜色都收敛到
`homework-reminder.html` 顶部 `:root` 的 46 个令牌（`--bg` / `--surface*` / `--ink*` /
`--muted*` / `--line*` / `--green*` / `--red*` / `--orange*` / `--accent-ink*` /
`--danger-ink` / `--warn-ink` / `--gloss` / `--edge` / `--chip` / `--glow-a|b` /
`--*-rgb` 三元组），一套主题就是这堆令牌的一组取值。这样自定义主题最坏只是配色难看，
不会把可读性搞崩（也不能注入任意 CSS）。

- **定义**：`themes.py`
  - `THEME_VARS`：令牌白名单 + 中文说明，导入时逐项校验（`#rrggbb` / `rgba(...)` /
    `"12, 34, 56"` 三种格式），出现白名单外的键直接报错。
  - `BUILTIN_THEMES`：`warm`（默认）/ `light` / `dark`，每套都写全令牌，不走派生。
  - `normalize_theme(raw, keep_id=)`：收拾用户导入的 JSON。**最少 3 个变量**即可，
    其余由 `_derive_vars()` 按 `scheme` 推导（浅色用 `_mix(surface, color, t)`，
    深色用 `_lighten`）。`keep_id=True` 用于读磁盘：文件里没写 id 时按
    `name|author` 算一个稳定摘要，否则每次启动都会随机出一个新 id，「活动主题」就对不上。
  - `ball_palette(theme)`：推导悬浮球用的 34 个 ARGB 角色（球渐变、角标、玻璃、
    文字、强调、关闭按钮、菜单）。强调类一律用 `--accent-ink` / `--danger-ink` /
    `--warn-ink` 这类「ink 版」令牌，因为 `--red` / `--orange` 在深色主题里是
    「深底上的底色调」，直接当文字色会糊。
  - `glass` 字段（`dark` / `light`）决定悬浮球概览卡片和右键菜单的玻璃层是深还是浅，
    浅色玻璃会把文字整体切成深色。
- **首屏防闪色**：`homework_server.py` 返回 HTML 时把
  `<style id="theme-vars">:root{...}</style>` 注入到 `<!--THEME_STYLE-->` 占位处
  （外加一行 `document.documentElement.dataset.scheme = ...`）。页面还没执行 JS
  就已经是对的颜色，不会先闪一下暖色再变深色。
- **前端切换**：`applyThemeVars()` 用 `root.style.setProperty` 覆盖，并先清掉上一次
  内联过的令牌（`themeState.appliedKeys`），避免换主题后残留某个旧值。
- **存储**：活动主题 id 存 `prefs.json` 的 `theme`；自定义主题存 `themes.json` 的
  `custom` 数组（上限 24 个，同名同作者再导入算覆盖更新）。
- **悬浮球同步**：`AppContext.on_theme_changed` 由 `desktop_app.py` 挂成
  `_apply_theme_to_ball()` → `BallWindow.set_palette(themes.ball_palette(theme))`，
  后者只换一个 dict 引用再投一次 `WM_BALL_DATA` 重绘（不重新拉数据，换主题不该发请求）。
- **启动兜底**：`prefs` 里的主题 id 找不到（主题被删、手改过文件）时，`AppContext`
  退回默认暖色并把 `prefs` 纠正掉，避免「一个都没选中」。

### 运行环境门槛（1.4.3）

主界面走 WebView2，所以「能跑」实际上由 Edge 的支持矩阵决定，不是由 Python 决定：

| 项 | 要求 | 由谁保证 |
| --- | --- | --- |
| CPU/系统位数 | 仅 64 位 | `installer.iss` 的 `ArchitecturesAllowed=x64compatible` |
| Windows 版本 | 10.0.17763（Win10 1809）及以上 | `installer.iss` 的 `MinVersion` |
| WebView2 Runtime | 必须存在 | 启动时 `ensure_webview2()` 自检 |
| .NET Framework | 4.7.2+（pythonnet 用） | Win10 1809 起系统自带 4.8 |

两道检查的分工：

- **安装期**：`MinVersion=10.0.17763` 直接拦下 Win7/8/8.1 —— Edge 从 109 版起不再支持这些系统，
  WebView2 自然也没有。打包进去的 Python 3.13 本身还支持 Win8.1，但 UI 层跑不起来，
  与其让用户装完发现闪退，不如在第一步就说清楚。
- **启动期**：`webview2_version()` 按 HKLM → WOW6432Node → HKCU 顺序读
  `Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` 的 `pv`。
  三处都要看：64 位常规安装写 HKLM、旧装机包可能只写 WOW6432Node、免管理员的
  Evergreen Standalone 写 HKCU。`pv` 为 `0.0.0.0` 视为未安装。
- 缺失时 `ensure_webview2()` 弹 `MB_YESNO | MB_ICONERROR | MB_TOPMOST`，点「是」用
  `webbrowser.open()` 打开 `https://go.microsoft.com/fwlink/p/?LinkId=2124703`，然后用返回码 3 退出。

**为什么必须自己检测**：程序是 `console=False` 的 windowed 应用，没有控制台；
pywebview 拿不到 WebView2 时只是抛异常，用户看到的就是「双击没反应」。
检测点放在 `main()` 里 `acquire_single_instance()` 之后、`hs.APP_HTML.exists()` 之前 ——
已有实例在跑时直接唤回，不做多余检查。

### 关键文件

| 文件 | 责任 |
| --- | --- |
| `desktop_app.py` | 桌面入口；无边框主窗口；标题栏桥接、缩放、DWM 圆角；悬浮球的生命周期、位置持久化、角标数据；托盘图标与自绘右键菜单；单实例与唤回；退出清理 |
| `themes.py` | 主题定义与派生：内置三套主题、`normalize_theme()` 校验、`ball_palette()` 悬浮球调色板 |
| `ball_window.py` | 悬浮球 / 右键菜单 / 概览卡片：同一个原生分层窗口（`WS_EX_LAYERED`）+ GDI+ 绘制，`UpdateLayeredWindow` 逐像素透明，自绘圆球/阴影/图标/角标；展开动画（`SetWindowPos` 缓动）、截屏虚化、命中与收起 |
| `shrink_anim.py` | 收起主窗口的「吸进悬浮球」动画：抓一帧窗口画面（`PrintWindow` 优先，屏幕截图兜底），用分层窗口 `UpdateLayeredWindow` 按 `easeInQuad` 逐帧缩到球的位置并淡出；`_FrameClock` 提供 16 ms 高精度节拍 |
| `homework_server.py` | 本地 HTTP API、登录、DPAPI、TronClass 数据归一化、完成状态 |
| `homework-reminder.html` | 主界面 HTML/CSS/JS；自绘标题栏、拖拽/缩放热区、隐形滚动条 |
| `ball.html` | 旧版悬浮球页面（WebView2 + CSS 画球）；已被 `ball_window.py` 取代，程序不再加载，仅留作历史参考 |
| `floating.html` | 1.1.0 的旧版 380×500 悬浮窗；已被悬浮球取代，不再引用、不打包（历史文件） |
| `homework-desk.ico` | 窗口图标与托盘图标 |
| `HomeworkDesk.spec` | PyInstaller 打包配置 |
| `installer.iss` | Inno Setup 安装脚本 |
| `ChineseSimplified.isl` | Inno Setup 简体中文语言文件 |
| `build.ps1` | 一键重新打包 EXE 和安装程序（产物到 `..\version`） |
| `release/` | 对外分发的安装包（只留当前版本）与 `SHA256SUMS.txt` |
| `preview.png` / `preview-ball.png` / `preview-menu.png` / `preview-card.png` | README 用的主界面、悬浮球、右键菜单、概览卡片预览图（演示数据，不含真实作业） |

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
| GET | `/api/homework/{id}/detail` | 作业正文（已清洗 HTML）+ 附件清单；服务端缓存 10 分钟 |
| POST | `/api/homework/{id}/attachments/{uploadId}/download` | 下载单个附件到桌面 |
| POST | `/api/homework/{id}/attachments/download` | 下载该作业全部附件到桌面 |
| POST | `/api/homework/{id}/complete` | 仅本机标记完成 |
| GET | `/ball.html`（`/ball`） | 旧版悬浮球页面（程序不再使用，路由保留） |

所有接口只监听 `127.0.0.1`，并检查 Host/Origin。密码不会返回给前端。

### 3.2.1 桌面桥 API（`js_api`，仅桌面版可用）

前端通过 `window.pywebview.api.*` 调用，`window_state` / `window_control` /
`window_resize` / `window_drag` 是自绘标题栏专用的新接口。

| 方法 | 参数 | 说明 |
| --- | --- | --- |
| `window_state` | `target` | 返回 `{ok, maximized}`，用于同步最大化/还原图标 |
| `window_control` | `target, action` | `minimize` / `toggle-maximize` / `close` / `hide`；主窗 `close` = 隐藏主窗 + 显示悬浮球 |
| `window_resize` | `target, edge` | 触发系统原生缩放；`edge` 为 `left/right/top/.../bottom-right` |
| `window_drag` | `target` | mousedown 时调用：记下「光标 − 窗口原点」，之后 60 fps 轮询光标 + `SetWindowPos` 跟随；靠 `GetAsyncKeyState(VK_LBUTTON)` 判断松手 |
| `toggle_floating` | — | 显示/隐藏悬浮球，返回 `{open}` |
| `get_floating_state` | — | 主界面轮询悬浮球是否显示 |
| `show_main` / `show_main_and_login` | — | 回到主界面（同时收起悬浮球） |
| `hide_floating` | — | 收起悬浮球 |
| `hide_to_tray` | — | 隐藏主窗 + 显示悬浮球，进程不退出 |
| `activate_main` | — | 收起悬浮球 + 显示并前置主窗 |
| `ball_move` | `dx, dy, final=False` | 拖拽：按 CSS 像素增量移动悬浮球（内部 `SetWindowPos`）；`final=true` 时同一次调用写盘 |
| `ball_position` | — | 返回悬浮球当前 `{ok, x, y}`（物理像素） |
| `ball_save_position` | — | 手动保存当前坐标（`ball_move(final=True)` 已覆盖，保留兼容） |
| `ball_open_main` | — | 单击悬浮球：回主界面 |
| `ball_hide` | — | 右键悬浮球：收起 |
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

### 3.5 作业正文与附件（1.4.2）

`/api/todos` 只返回 `id / title / course_name / end_time / type` 五个有用字段，
**没有正文也没有附件**。正文和附件必须再按活动 id 查详情：

| 用途 | 上游接口 | 要点 |
| --- | --- | --- |
| 作业详情 | `GET /api/activities/{activity_id}` | 正文在 `data.description`（富文本 HTML），附件在 `uploads[]`；`uploads` 元素形如 `{id, name, size, type, allow_download, key}` |
| 附件原文件直链 | `GET /api/uploads/{upload_id}/url` | 返回 `{url}`；指向 `/download/file/{key}`，**原文件**，文件名与格式不变（首选） |
| 附件转码 PDF 直链 | `GET /api/uploads/document/{upload_id}/url` | 返回 `{status, url}`；指向 `/download/processed/{key}.pdf`，只作兜底 |

踩过的坑：

- **两条直链接口给的东西不一样，别选错**：
  - `/api/uploads/document/{id}/url` → `/download/processed/{key}.pdf`，是平台转码后的 **PDF**；
  - `/api/uploads/{id}/url` → `/download/file/{key}`，是**原文件**（docx 还是 docx）。
  1.4.1 只用了前者，导致 docx 存成 `.pdf`；1.4.2 起先取原文件接口，失败才退回 PDF 兜底。
  签名是按路径算的，自己拼 `download/original/...` 一定 403，必须走接口换链。
- 前端点击下载走 `POST`，但**请求体没人读**。`BaseHTTPRequestHandler` 不会替你把 body
  丢掉，剩余字节会留在 keep-alive 连接里，下一个请求行被解析成 `{}POST ...` 直接 501。
  现在下载路由开头统一 `self._discard_body()`。
- 附件名里的中文从 `Content-Disposition` 取出来是乱码（服务端把 UTF-8 字节塞进 header，
  requests 按 latin-1 解析）。`_filename_from_header()` 做了 latin-1 → utf-8 的还原；
  实际落盘优先用 `uploads[].name`，基本用不到 header。
- 正文 HTML 必须清洗再塞进 `innerHTML`：`sanitize_description_html()` 用 `HTMLParser`
  按白名单重建标签（p/br/列表/表格/a[href]/img[src] 等），丢弃 script/style/iframe/表单
  和一切 `on*` 属性、行内样式；`javascript:` 链接退化成纯文本 `<span>`。
- 详情缓存 10 分钟（`AppContext._detail_cache`），手动刷新（`refresh=1`）时前端清空
  `state.details` 重新取；列表预取只用两个并发，失败静默。

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
- `..\version\作业备忘-安装程序-1.4.3.exe`：安装程序（Inno Setup，`OutputDir=..\version`）

### 5.3 单独构建

```powershell
py -3.14 -m pip install requests pywebview pyinstaller
py -3.14 -m PyInstaller --noconfirm --clean HomeworkDesk.spec
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" .\installer.iss
```

> 本机 `py -3.14` 目前**没有装 PyInstaller**（直接跑会报 `No module named PyInstaller`），
> 打包时用的是临时虚拟环境 `%TEMP%\hd-venv` 里的解释器：
> `"%TEMP%\hd-venv\Scripts\python.exe" -m PyInstaller --noconfirm HomeworkDesk.spec`。
> 想用系统解释器的话先执行一次 `py -3.14 -m pip install pyinstaller`。
>
> 另外：如果 `dist\HomeworkDesk\HomeworkDesk.exe` 正在运行，目录会被锁住无法重建，
> 可以先 `--distpath dist_build` 打一份，或者关掉正在运行的实例。

### 5.4 版本升级

1. 修改 `homework_server.py` 的 `APP_VERSION`。
2. 修改 `installer.iss` 的 `MyAppVersion`、`OutputBaseFilename`、`VersionInfoVersion`。
3. 更新 `CHANGELOG.md`。
4. 重新运行 `build.ps1`。
5. 用新安装包覆盖安装；`%LOCALAPPDATA%\HomeworkDesk\data` 会保留。

### 5.5 发布

构建产物落在仓库外的 `..\version\`，仓库里**只保留当前版本**的安装包：

```powershell
# 1. 构建，输出 ..\version\作业备忘-安装程序-1.4.3.exe
.\build.ps1

# 2. 换掉 release 里的旧包
Remove-Item .\release\*.exe
Copy-Item ..\version\作业备忘-安装程序-1.4.3.exe .\release\

# 3. 重算校验值
#    文件名含中文，必须用 UTF-8 无 BOM 写，否则在别的编辑器里会变成 ???? 
$name = (Get-ChildItem .\release\*.exe).Name
$hash = (Get-FileHash ".\release\$name" -Algorithm SHA256).Hash
[System.IO.File]::WriteAllText("$PWD\release\SHA256SUMS.txt", "$hash  $name`n",
    (New-Object System.Text.UTF8Encoding $false))
```

安装包约 13 MB，小于 GitHub 单文件限制，可以随 `release/` 一起提交；
更推荐放进 GitHub Releases 作为附件，git 历史里就不会反复堆积二进制。

---

## 6. 已验证场景

本机已实际测试：

- 真实深大账号首次登录成功，并同步到待办数据。
- 删除会话文件后重启服务，自动使用 DPAPI 加密凭据重新登录成功。
- 主界面加载真实数据、刷新成功。
- 悬浮球置顶显示、80×80、不进任务栏、不抢焦点。
- 关闭主窗口后主窗隐藏、悬浮球出现，进程和本地服务继续在后台运行。
- 托盘图标可重新打开主界面、切换悬浮球，并从托盘菜单真正退出。
- 重复双击快捷方式会唤回已收起的窗口，不会产生第二个实例。
- 安装程序创建桌面、开始菜单和 Startup 快捷方式，路径指向安装后的 EXE。
- 图形验证码前端流程（显示、重试）通过模拟接口验证。

悬浮球验证（原生分层窗口版）：

| 检查项 | 结果 |
| --- | --- |
| 窗口尺寸 | 80×80（`GetWindowRect` 实测，打包版同样） |
| 逐像素透明 | 四角 4×4 采样与“关掉球之后”的桌面像素完全一致（差异 0/64） |
| 球体绘制 | 中心区域 400 个采样点中 >300 与背景不同 |
| 窗口样式 | `WS_EX_LAYERED` + `WS_EX_TOPMOST` + `WS_EX_TOOLWINDOW` + `WS_EX_NOACTIVATE` |
| 主界面标题栏 × | 主窗隐藏、悬浮球出现、进程存活 |
| 拖拽 | 起点 (500,400) 拖到 (400,430) → 窗口位移正好 (-100, +30) |
| 松手落盘 | `ball-state.json` 与窗口实际坐标一致 |
| 收起 / 再显示 | 回到上次位置 |
| 单击悬浮球 | 展开概览卡片（点卡片里的作业行才回主界面） |
| 右键悬浮球 | 弹出操作菜单，进程不退出 |
| 主界面「悬浮球」按钮 | 一次点击显示、再点收起 |
| 角标数字 | 未完成 11 条 → 红底白字「11」 |
| 快照（打包版 exe） | 启动 → `WM_CLOSE` → 球在 (1582,265)，截图可见绿色圆球 + 文档图标 + 红色角标，四周透出背景文字 |

> 验证时用截图比对：先截“有球”的画面，再 `hide_floating()` 截“无球”的画面，
> 两者在球外区域必须一致。直接按像素总数比对会被动态壁纸/视频干扰，所以只看四角。

右键菜单 + 概览卡片（2026-10 新增，离屏自动化）：

窗口停在 (-600, -600)、假装工作区在屏幕外，用 `PostMessage` 打真实鼠标消息走完整
`WndProc`，22 项断言全通过（脚本：`%TEMP%\hd_menu_test.py`）：

| 检查项 | 结果 |
| --- | --- |
| 右键展开 | 80×80 → 196×170，`_popup_kind == "menu"` |
| 菜单尺寸 | 宽 `MENU_W`、高 `PAD*2 + 4 项 + 2 分隔线`，与算式一致 |
| 命中 | 首项 0、分隔线 -1、左边距 -1 |
| 点「隐藏悬浮球」 | 回调收到 `hide`，窗口回到 80×80 |
| 左键展开 | 80×80 → 328×234，`_popup_kind == "card"` |
| 点作业行 | 回调收到 `open-main`，随后收起 |
| 收起按钮 / Esc / 点外部 | 均回到 80×80 |
| `hide()` / `show()` 投递 | `IsWindowVisible` 正确切换，且回来时仍是 80×80 |

视觉用「把分层窗口的 DIB 拷成 PNG」检查（`%TEMP%\hd_preview\*.png`）：
深色玻璃层能透出虚化后的桌面、文字不溢出、右上角 × 和菜单图标对齐。

动画帧率（2026-10，`%TEMP%\hd_fps_test.py`）：

| 指标 | 改造前 | 改造后 |
| --- | --- | --- |
| 定时器间隔中位 | 30.4 ms（32 fps） | 16.2 ms（61 fps） |
| 间隔 > 20 ms 的比例 | 62% | 0% |
| 最大间隔 | 32.0 ms | 16.7 ms |
| 单帧渲染耗时 | 3.0 ms（不是瓶颈） | 3.0 ms |
| 每帧渲染次数 | 1.11 | 1.07 |

主窗口过渡（`%TEMP%\hd_anim_live.py`，窗口摆在 (-3000,-1200) 屏幕外）：

| 检查 | 结果 |
| --- | --- |
| `typeof window.__appAnim` | `function` |
| 进场 opacity 采样（每 50 ms） | 0.23 → 0.69 → 0.84 → 0.96 → 0.99 → 1 |
| 退场 opacity 采样 | 0.95 → 0.78 → 0.37 → 0 → 0（停住，下次显示不会闪旧内容） |
| 再次进场 | 0.65 → 1（能正常重播） |
| 类名切换 | `app-enter` / `app-leave` 正确挂到 `<html>` |
| 计算样式 | `app-titlebar`、`page-shell` 的 `animation-name` 均为 `app-window-in` |

拖动 / 缩放 / 收起动画（2026-10-28，`%TEMP%\hd_drag_live.py`、`hd_resize_live.py`、`hd_e2e_hide.py`）：
窗口一律摆在 (-2400,-1400) 屏幕外，用 `mouse_event` 打真实鼠标按键，避免打扰桌面。

| 检查 | 结果 |
| --- | --- |
| 按住标题栏拖 (5,5)→(105,55) | 窗口 `dx=100 dy=50`，尺寸不变 |
| 拖右下角缩放 (+100,+50) | 宽高各 +100 / +50，左上角不动 |
| `typeof window.pywebview.api.window_drag` | `function` |
| `hide_to_tray()` 全程耗时 | 0.33 s（含 0.28 s 收缩动画） |
| 收缩动画窗口 | 出现过 1 次，播完已销毁（`FindWindowW("Static", "HomeworkDeskShrink") == 0`） |
| 收起后 | 主窗口隐藏、球可见且仍是 80×80 |
| `activate_main()` | 主窗口回来、球收起；连做两轮都正常 |
| `ShrinkAnimation` 离屏基准 | 15 帧 / 0.305 s，帧间隔中位 16.0 ms、最大 16.5 ms，首帧 900×600 alpha 255 → 末帧 80×80 alpha 0 |

> 教训：`WM_NCLBUTTONDOWN + HTCAPTION / HTLEFT` 在 pywebview 的 frameless 窗口上
> **不会**启动系统移动 / 缩放循环（实测窗口一动不动），`ReleaseCapture()` 也只对
> 本线程的鼠标捕获有效。拖动和缩放都只能自己轮询光标 + `SetWindowPos`。

窗口改版（源码预览 + 重新打包的 EXE）额外验证：

- 无边框窗口正常显示自绘 38px 标题栏（图标、标题、三个窗口按钮），无系统白条。
- 客户区尺寸补回 1180×830，标题栏不遮挡页面内容。
- 四边、四角缩放走系统原生循环；最大化贴合工作区、不盖任务栏，还原后恢复圆角。
- 主页面滚动条为“滚动浮现、约 1 秒淡出、右侧热区唤出、悬停墨绿”。
- 托盘图标创建成功；隐藏后进程、本地服务、自动登录状态都保留。
- 托盘“退出作业备忘”能在后台线程正常结束 `webview.start()` 并退出进程
  （用独立测试脚本调用 `_quit_from_tray()` 验证）。
- 打包后的 `HomeworkDesk.exe` 同样通过上述关闭→托盘→唤回→退出流程。

自绘托盘菜单（2026-10 版）额外验证：
- 直接用 `TrayMenuPopup._paint(Graphics)` 渲染三种状态（无悬停 / 悬停普通项 /
  悬停退出项）并放大检查，圆角、图标、文字基线、分隔线都正常。
- 在真实 `TrayController` 上模拟右键：菜单成功弹出（218×176），文案随
  `float_state` 变成「收起悬浮窗」，点击该项触发回调并自动关闭菜单。
- 安装目录里的 `HomeworkDesk.exe` 用真实鼠标操作复测：

  | 操作 | 结果 |
  | --- | --- |
  | 托盘图标右键 | 弹出 218×176 圆角卡片菜单，位于光标上方 8px、右对齐图标 |
  | 鼠标移到「打开主界面」 | 出现淡墨绿圆角胶囊高亮，图标与文字不变色 |
  | 点「显示悬浮窗」 | 菜单先关闭，悬浮窗出现并置顶；再开菜单文案变「收起悬浮窗」 |
  | 左键单击图标 | 主窗口回到前台（不弹菜单） |
  | Esc | 菜单关闭，主界面不受影响 |
  | 点「退出作业备忘」 | 日志写「从托盘退出应用。/ 桌面应用退出。」，进程结束 |

- 托盘菜单关闭方式：Esc、点菜单外（`Deactivate` + 250ms 宽限）。
- 小坑：菜单坐标依赖光标位置（`show_at` 用光标点向上弹），自动化点击时不要
  假设固定的窗口位置，先按窗口尺寸（218×176）找到弹窗再算行坐标。

1.2.3 回归验证（悬浮球左右键失灵这条）：

| 检查 | 结果 |
| --- | --- |
| `import ball_window, shrink_anim` 后 `SetWaitableTimer.argtypes` | 两边都是 `c_void_p`，各自的结构体都能成功调用（返回 1） |
| 主窗口点 × → 收起 | 收缩动画播完、主窗口隐藏、球出现在 `(1661,311)` 80×80 |
| 左键单击球 | 展开卡片 328×234 |
| 右键（卡片展开态） | 收回 80×80 |
| 右键（球态） | 弹出菜单 196×170 |
| 真实鼠标拖动 (−120,+90) / (+120,−90) | 位置精确跟手，尺寸保持 80×80，松手写回 `ball-state.json` |
| 日志 | 无 `逐帧时钟线程异常`、无 `显示托盘提示失败` |
| 安装版 `HomeworkDesk.exe`（1.2.3） | 启动日志 `启动作业备忘桌面版 v1.2.3`；点 × → 球；左键展开 / 右键菜单全部正常 |
| 安装包静默安装 | 退出码 0、150 个文件、`_internal/homework-reminder.html` 与源码字节级一致 |

> 排查手法（下次直接照做，别猜）：`WindowFromPoint` 确认球中心命中的是
> `HomeworkDeskBallWindow` → `SendMessageTimeoutW(WM_NULL, 400ms)` 确认窗口线程
> 还在泵消息 → 两者都正常就开 `HOMEWORK_DESK_DEBUG=1` 看 `_wndproc` 吞掉的异常。
> 这次的异常信息就躺在日志里：`expected LP__LARGE_INTEGER instance instead of
> pointer to _LARGE_INTEGER`。

1.3.1 主题功能验证：

| 检查 | 结果 |
| --- | --- |
| `GET /api/themes` | 返回 warm / light / dark 三套 + 自定义主题，`active` 与 `prefs.json` 一致 |
| `POST /api/themes/active` | 切 light / dark / 自定义全部返回 `ok`，`vars` 完整；页面无需刷新即变色 |
| `POST /api/themes/import` | 只写 6 个变量的「樱」导入成功并自动补齐派生色；同名同作者重复导入不增项；`--bg` 写成 `red` 返回 `--bg 需要是 #rrggbb 形式的颜色。` |
| 首屏防闪色 | 页面 HTML 内已含 `<style id="theme-vars">`，改成深色后重开窗口第一帧就是深色 |
| 深色主题可读性 | 第一版深色下「已逾期 / 紧急」等红绿文字几乎看不见；改用 `--accent-ink` / `--danger-ink` / `--warn-ink` 后复检正常（任务卡里的「计算机视觉 / 作业 / 紧急」等 chip 也清楚） |
| 主题面板 | 三套内置 + 自定义共 4 张卡片，当前项带绿框「使用中」，自定义项右下角删除按钮点一次变「删除？」、再点才删 |
| 悬浮球跟随主题 | 樱（`--green: #8b3f5a`）→ 球体变粉；深色 → 球体偏深绿；运行中切换主题卡片玻璃深浅立即重绘（浅色玻璃自动切成深字） |
| 未登录概览卡片 | 修复前中间空一块；现在显示「还没登录深大优课 / 打开主界面登录后会自动同步」 |
| 手写 `themes.json`（无 id） | 按「名字 + 作者」生成稳定 id，重启后仍能选中；`prefs` 里 id 失效时退回 warm 并纠正 `prefs` |

> 主题相关排查：界面「卡片全没了 / 一个都没选中」先看控制台有没有 JS 异常
> （曾经因为 `themeState.appliedKeys` 没初始化，`applyThemeVars` 直接抛异常，
> 整张面板渲染成空）；悬浮球颜色不跟随就查 `context.on_theme_changed` 是否在
> `create_windows()` 之后被挂上（挂之前球用 `DEFAULT_PALETTE`）。

1.4.1 作业内容 / 附件下载验证（真实账号，11 条待办）：

| 检查 | 结果 |
| --- | --- |
| `GET /api/activities/{id}` 可用性 | 11 条待办全部 200；正文 43–3736 字不等，其中 5 条为空（老师没写说明） |
| `GET /api/homework/{id}/detail` | 返回 `descriptionHtml / descriptionText / attachments`；附件数量与上游一致（0–4 个） |
| 正文清洗 | `script` / `onclick` / `onerror` / `javascript:` / `iframe` 全部剔除；`<br>`、表格、`<a href>` 保留；脏 HTML（未闭合 `<p>`）清洗后标签完全配对 |
| 单个附件下载 | docx → 桌面得到 `第二章作业(1).docx`（12057 B，`PK` 头，与平台 `size` 一致） |
| 原文件附件 | zip / md / 图片走 `download/file/{key}`，字节数与上游 `size` 一致（md 3597 B） |
| 全部下载 | 2 个附件的作业一次存下 `.docx` + `.pptx`，失败的单个不影响其他 |
| 重名处理 | 连续下载 3 次得到 `(1) (2)`，桌面已有文件不被覆盖 |
| 桌面路径 | `SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)` → `C:\Users\JASISION\Desktop`，OneDrive 重定向也跟得上 |
| keep-alive 污染 | 修复前：单发成功后紧接着的批量请求返回 501（`{}POST`）；修复后连发两个 POST 均 200 |
| 展开状态保持 | 展开后等 35 秒（超过 28 秒自动刷新阈值），卡片仍保持展开、正文不丢 |
| 后台预取 | 首屏 11 张卡片在 ~2 秒内补齐「N 个附件」，点开直接出内容无需等待 |

1.4.3 运行环境检测验证：

| 检查 | 结果 |
| --- | --- |
| 注册表探测 | 本机读到 `pv=156.0.4314.50`（HKLM WOW6432Node），`ensure_webview2()` 直接返回 True，不弹窗 |
| 缺失 + 点「否」 | 打桩 `MessageBoxW` 返回 7（IDNO）→ 函数返回 False，`webbrowser.open` 未被调用 |
| 缺失 + 点「是」 | 打桩返回 6（IDYES）→ 打开 `go.microsoft.com/fwlink/p/?LinkId=2124703`，函数仍返回 False（不继续启动） |
| 安装拦截 | `MinVersion=10.0.17763` 通过 ISCC 编译，写入 setup；Win7 实机未验证（无环境），依据 Inno 官方语义 |
| 32 位系统 | `ArchitecturesAllowed=x64compatible`，安装向导第一步拒绝；未实机验证 |

1.4.2 原样下载验证（真实账号，12 个附件全量）：

| 检查 | 结果 |
| --- | --- |
| 原文件接口 | `GET /api/uploads/{id}/url` 对 12 个附件全部 200，返回 `/download/file/{key}` 签名链接 |
| 字节数一致性 | 12 个附件下载后的大小与上游 `uploads[].size` **逐一相等**（docx 12057 / 494418、pptx 308694、ppt 177628、zip 63709280、md 3597、pdf 179402…） |
| 格式一致性 | doc/docx/pptx/ppt 均以 `PK\x03\x04`（或 OLE `D0 CF 11 E0`）开头，即真实 Office 文件，无一个被转成 PDF |
| 兜底路径 | 把原文件接口改成不存在的路径后，自动退回 `document/url` 得到 `.pdf` 并置 `converted=true`；恢复后立即回到 `.docx` |
| UI 提示 | 无头 Edge 点击下载，toast 为「已保存到桌面：自然语言处理 期末大作业格式(1).doc」，不再出现 PDF 后缀或转码说明 |

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
找到作业备忘图标**单击**即可打开主界面；也可以在“设置 → 个性化 → 任务栏”里把该图标设为常显。
要真正结束进程：托盘图标**右键** →「退出作业备忘」，或任务管理器结束 `HomeworkDesk.exe`。

### 7.3.2 托盘没有图标 / 右键菜单异常

- 先确认进程还在（任务管理器里能看到 `HomeworkDesk.exe`）。
- 日志里应有 `托盘图标已创建`；若为 `创建托盘图标失败`，把日志和 Windows 版本一起反馈。
- 图标可能只是被 Win11 收进了溢出区；拖出来即可常显。
- 菜单弹不出来时看日志里有没有 `显示托盘菜单失败` / `绘制托盘菜单失败`。
  菜单是自绘 Form，若出现空白方块，多半是新加的绘制代码抛异常——
  回调里的异常只写日志，不会弹窗。
- 想调整菜单外观，改 `desktop_app.py` 顶部的 `TRAY_MENU_*` 常量和 `TRAY_COLORS`，
  不要改回 `ContextMenuStrip`：本环境 pythonnet 不回调重写的 `OnRender*`。

### 7.3.3 悬浮球不见了 / 样子不对

| 现象 | 先看这里 |
| --- | --- |
| 关掉主界面后桌面没有球 | 日志是否有 `显示悬浮球失败`；托盘菜单的「显示悬浮球」能否唤出；球可能被拖到了某个显示器边缘，删掉 `data\ball-state.json` 后重新显示即回到默认位置（右上角）。 |
| 球出现在屏幕外、点不到 | `_position_ball()` 会用 `clamp_ball_position()` 夹回工作区；如果当时那块显示器已经拔掉，删 `ball-state.json` 即可。 |
| 球周围有一块方形浅灰/黑底 | 说明 `UpdateLayeredWindow` 没生效：确认窗口带 `WS_EX_LAYERED`，且位图是 `Format32bppPArgb`（用 `Argb` 会算错 alpha 预乘，边缘发黑）；圆外必须真正清成 `Color.Transparent`。 |
| 球边缘有黑边 / 锯齿 | 同上，多为 `PArgb` 与 `Clear(Color.Transparent)` 被改动，或 `SmoothingMode` 不是 `AntiAlias`。 |
| 球能看见但点不动 / 点别处就消失 | 检查 `WM_MOUSEACTIVATE` 是否返回 `MA_NOACTIVATE`；返回默认值会把前台窗口切走。 |
| 拖动跟不上鼠标 / 越拖越偏 | 拖拽必须是 `_drag_origin + (cursor - _drag_start)` 的绝对位移；改成累加增量或在前端取整会漂。 |
| 重启后球回到旧位置 | 松手时 `move_to(..., notify=True)` 才写 `ball-state.json`；确认该文件可写。 |
| 启动时球在屏幕外找不到 | 窗口创建后立刻 `move_to()` 到 `clamp_ball_position()` 的结果；如果保存的坐标来自已拔掉的显示器，删 `ball-state.json`。 |
| 点击球后全屏游戏被切出来 | 检查 `WS_EX_NOACTIVATE` 是否被抠掉，以及 `activate_main()` 是否被误触发。 |

### 7.3.4 右键菜单 / 概览卡片异常

| 现象 | 先看这里 |
| --- | --- |
| 右键球没有反应 | 确认 `BallWindow(menu_provider=...)` 传了；`_load_menu_items()` 会丢掉没有 `label` 的项，并在首尾去掉多余分隔线，如果全被丢掉就什么都不弹（日志有 `右键菜单没有可用项`）。 |
| 右键后球直接收起 | 旧版行为是「右键收起」；现在 `WM_RBUTTONUP` 分支必须是 `open_menu()`。日志里若出现 `_on_hide` 调用路径说明拿的还是旧包。 |
| 菜单瞬间自己关掉 | 展开完成后 `TIMER_WATCH` 每 40 ms 轮询全局键；如果窗口还没真正显示就被判定「在窗口外」会立刻收起。检查 `_finish_animation()` 里 `_win_rect` 是否已经等于目标矩形。 |
| 菜单/卡片是纯黑方块，看不到虚化背景 | `_capture_backdrop()` 失败（多显示器负坐标、远程桌面、护眼软件抓屏受限）会回退成纯深色，功能不受影响；日志 `截取卡片背景失败`。 |
| 文字模糊或发虚 | 检查 `TextRenderingHint` 是否为 `AntiAlias`；另外卡片是逐像素 alpha 合成，字体不要用 `ClearTypeGridFit`（子像素抗锯齿在透明通道上会出彩边）。 |
| 卡片高度和内容对不上 | 展开时按 `_row_count` 固定行数，数据在展开期间更新只会换文字、不改行数；要改行数必须先 `collapse()`。 |
| 高分屏下卡片很小 | `_ui_scale` 在窗口创建后由 `GetDpiForWindow()` 决定；如果进程不是 DPI aware，会拿到 96。检查 `enable_dpi_awareness()` 是否在 `webview.start()` 之前调用。 |

### 7.3.5 动画卡顿 / 窗口过渡

| 现象 | 先看这里 |
| --- | --- |
| 展开动画一顿一顿、帧数少 | 先量一下：`%TEMP%\hd_fps_test.py` 会打印定时器实际间隔。中位 30 ms 说明跑的还是 `SetTimer(16)` 老路径（被 15.625 ms 节拍量化），应走 `AnimClock`；中位 16 ms 才正常。 |
| 动画帧率正常但一卡一卡 | 看 `每帧 SetWindowPos+渲染` 耗时；卡片态渲染约 3 ms，明显更高通常是 `_capture_backdrop()` 被放进了循环，或 `WM_PAINT` 没在动画期间跳过（每帧画两次）。 |
| `AnimClock` 线程报错 / 定时器创建失败 | 日志若出现 `逐帧时钟不可用，退回 SetTimer 驱动动画`，检查 `kernel32.CreateWaitableTimerExW` / `SetWaitableTimer` 的原型是否被别的模块覆盖（见 7.3.7）。现在两条路都有兜底：高精度定时器失败退 `SetTimer(10)`，再失败靠 `_recover_stale_anim()` 强制收尾，动画只会变糙不会卡死。 |
| 主窗口打开/收起没有动画 | 页面里 `window.__appAnim` 是否定义（`homework-reminder.html` 末尾那段）；`DesktopBridge._play_main_anim()` 里 `evaluate_js` 失败只写 debug 日志，不报错。 |
| 主窗口出现时白屏一下 | 退场动画用 `both` 停在 `opacity: 0`，下次显示必须靠 `enter` 才能看见内容；`activate_main()` 和 `events.shown` 都会调，页面侧 300 ms 去重。若两条路径都没走到，检查 `_on_window_shown` 是否被替换掉。 |
| 收起时窗口“啪”地消失 | 现在收起走 `shrink_anim.py`：`hide_to_tray()` 先 `ShrinkAnimation.from_window()` 抓帧，`play(on_ready=...)` 的第一帧画好后才隐藏真窗口。如果直接消失，说明抓帧返回了 `None`（走了兜底分支），日志里会有 `抓取主窗口画面失败` 或 `屏幕截图也失败了`。 |
| 动画在窗口还没显示时就跑完了 | 页面会检查 `document.visibilityState`，不可见时把请求挂在 `pending`，等 `visibilitychange` 变可见再播。 |

### 7.3.6 标题栏拖不动 / 边缘缩放没反应

| 现象 | 先看这里 |
| --- | --- |
| 按住标题栏窗口不动 | 页面 `mousedown`（capture 阶段）应调用 `window.pywebview.api.window_drag('main')`。用 DevTools 看 `typeof window.pywebview.api.window_drag` 是否为 `function`；不是就是后端方法没暴露（名字必须不以 `_` 开头）。 |
| `window_drag` 返回 `{ok: false}` | 回到 Python 时左键已经松开了（`GetAsyncKeyState(VK_LBUTTON)` 的 0x8000 位没了）。常见于 IPC 很慢或只是一次单击；按住再拖即可。 |
| 拖起来一跳一跳 | `_drag_loop` 里 `time.sleep(0.008)` 实际会被系统节拍量化成 ~15.6 ms（≈64 fps），属正常；若明显更慢，检查是不是又退回了「每帧一次 IPC」的老路。 |
| 按边缘拖不动大小 | 热区在 `.win-resize-layer` 里，必须有 `data-resize="left/right/top/bottom/top-left..."`；`window_resize()` 只接受 `RESIZE_EDGES` 里的键。 |
| 缩放后窗口比设定的小 / 大 | 下限是 `WINDOW_MIN_WIDTH/HEIGHT`（900×620），必须和 `create_window(min_size=...)` 一致，否则松手后会被 pywebview 拉回。 |

### 7.3.7 悬浮球左右键全失灵 / 拖不动

**症状**：球看得见、鼠标移上去光标也变成手型，但左键点不开卡片、右键不弹菜单、按住也拖不动；
`%LOCALAPPDATA%\HomeworkDesk\data\homework-desk.log` 里能搜到
`逐帧时钟线程异常` / `ctypes.ArgumentError: ... expected LP__LARGE_INTEGER`。
（1.2.2 就是这个毛病，1.2.3 已修。）

**根因**：`ctypes.windll` 是**进程级共享**的。`ctypes.windll.kernel32` 是一个单例，
`SetXxx.argtypes` 谁最后设置谁生效，模块之间会互相覆盖。`ball_window.py` 和
`shrink_anim.py` 各自定义了 `_LARGE_INTEGER` 结构并各自声明
`SetWaitableTimer` 的原型；`desktop_app.py` 先 import `ball_window`、后 import
`shrink_anim`，于是球这边传自己的结构体时类型对不上，动画时钟线程第一帧就抛异常退出，
`_animating` 永远停在 `True`，`expand()` / `collapse()` 全部被 `if self._animating: return`
静默吞掉 —— 表现就是球“哑了”。

**规则（踩过三次了，务必遵守）**：凡是**两个及以上模块都会声明**的 Win32 函数，
参数里的结构体指针一律写成 `ctypes.c_void_p`，调用处显式
`ctypes.cast(ctypes.byref(obj), ctypes.c_void_p)`。已经统一过的有：

| 函数 | 结构体参数 |
| --- | --- |
| `gdi32.CreateDIBSection` | 第 2 个（`BITMAPINFO*`） |
| `user32.UpdateLayeredWindow` | 第 8 个（`BLENDFUNCTION*`） |
| `kernel32.SetWaitableTimer` | 第 2 个（`LARGE_INTEGER*`） |

**排查手法**：不用猜。先确认窗口本身是好的 —— 用 `WindowFromPoint` 看球中心命中的是谁；
再 `SendMessageTimeoutW(hwnd, WM_NULL, ..., 400ms)` 判断窗口线程还在不在泵消息；
两者都正常但点击没反应，就基本可以断定是 `_wndproc` 里被条件挡掉或异常被吞了，
这时把日志级别开到 DEBUG（`HOMEWORK_DESK_DEBUG=1`）重跑，异常会落盘。

**自愈逻辑**：`BallWindow._recover_stale_anim()` 在 `expand` / `open_menu` / `collapse`
入口处检查「动画已超时（时长 + 0.6 s）仍未收尾」，是就强制 `_finish_animation()`。
即使以后再有时钟类故障，最坏只是动画变生硬，不会让整个球失去交互。

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
  - 托盘菜单的「显示/收起悬浮窗」文案是否跟当前状态一致；
  - 悬浮球关闭 / 收起是否只隐藏球、主界面能正常回来；
  - 悬浮球是否 80×80、透明、置顶、不进任务栏；
  - 拖动后 `ball-state.json` 是否跟着更新；
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
- [x] 80×80 透明悬浮球、主界面 ⇄ 悬浮球切换、拖拽与位置记忆（步骤 2）
- [x] 使用文档、更新日志、交接文档
- [x] 安装包和 SHA256 校验值
- [ ] 开源许可证（当前未指定，公开发布前需决定）
- [ ] 代码签名（可选，正式分发建议）

---

## 10. 对外发布前检查

```text
1. 仓库内没有 .log、.dat、data、dist、build、dist_installer。
2. release/ 里只有当前版本的安装程序，SHA256SUMS.txt 的哈希与实际文件一致。
3. README 和 HANDOVER 中没有个人路径、用户名、真实作业数据。
4. 安装包的版本号、文件名和 CHANGELOG 一致。
5. 用一台没有登录过本应用的 Windows 账户做一次冷启动测试。
```

> 2026-10-27 已把 `build/`、`dist/`、`__pycache__/` 移到项目目录**同级**的
> `_not-for-github/`（内含说明 README）。用网页拖拽上传时，不要把这个文件夹
> 一起拖进去；用 `git` 上传时它们本来就被 `.gitignore` 忽略。

## 11. 上传到 GitHub

仓库已经建好并完成首次提交：

| 项 | 值 |
| --- | --- |
| 远程 | `https://github.com/JASISOON/homework.git` |
| 分支 | `main`（跟踪 `origin/main`） |
| 当前版本 | 1.4.3 |

### 日常更新流程

```powershell
git status                 # 确认改动范围，data/、dist/、build/ 应被忽略
git add -A
git commit -m "feat: ..."  # 提交信息写清楚改了什么、为什么
git push origin main
```

推送前先确认这几点：

- `git status` 里没有 `data/`、`*.log`、`credentials.dat`、`session.dat`；
- `release/` 里只有一个安装包，且和 `SHA256SUMS.txt` 对得上；
- 版本号（`APP_VERSION` / `installer.iss` / `CHANGELOG.md`）三处一致。

### 在 Releases 里发布

1. GitHub 仓库 → Releases → Draft a new release，tag 填 `v1.4.3`。
2. 附件上传 `release/作业备忘-安装程序-1.4.3.exe`。
3. 说明直接复制 `CHANGELOG.md` 里对应版本的段落。
4. 正文里附上 `SHA256SUMS.txt` 的哈希值。

> 仓库里带中文文件名的安装包会让仓库体积随版本线性增长，所以只在 `release/`
> 保留最新一版；旧版本从 git 历史或 Releases 里取。

当前仓库**没有附带开源许可证**。如果准备公开源码，发布前需要明确选择
MIT / Apache-2.0 / 私有仓库等授权方式；如仅自用，保持私有仓库即可。

当前仓库**没有附带开源许可证**。如果准备公开源码，发布前需要明确选择
MIT / Apache-2.0 / 私有仓库等授权方式；如仅自用，保持私有仓库即可。


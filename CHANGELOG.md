# Changelog

## [1.2.3] - 2026-10-28

### Fixed

- **悬浮球左右键突然全部失灵、也拖不动**（1.2.2 引入的严重回归）。根因是
  `ctypes.windll` 进程级共享：`ball_window.py` 和 `shrink_anim.py` 各自声明了
  `kernel32.SetWaitableTimer` 的原型，且都写成 `POINTER(自己的 _LARGE_INTEGER)`。
  `desktop_app.py` 先 import `ball_window`、后 import `shrink_anim`，后者的声明
  把前者的覆盖掉 —— 悬浮球的 60 fps 动画时钟线程第一帧就抛
  `ctypes.ArgumentError: expected LP__LARGE_INTEGER ...` 并退出，
  `_animating` 永远停在 `True`，之后 `expand()` / `collapse()` 全被
  `if self._animating: return` 静默吞掉，表现就是点左键不展开、点右键不弹菜单。
  现在两处原型统一声明成 `c_void_p`，调用处显式 `cast(byref(...), c_void_p)`。
  （同一类问题 1.2.2 已经踩过 `CreateDIBSection` 和 `UpdateLayeredWindow`，这次是
  第三个函数，已在代码注释里写清楚原因。）
- **动画永远不会卡死**：`AnimClock` 一旦建不出高精度定时器或线程异常，就自动退回
  `SetTimer(10)` 继续出帧；另外 `expand` / `open_menu` / `collapse` 入口都有
  「动画超时（时长 + 0.6 s）未收尾就强制复位」的自愈逻辑。任何时钟故障都只会
  让动画降级，不会再让悬浮球彻底失聪。
- **第一次收起到托盘时的气泡提示不显示**：`ShowBalloonTip` 的第 4 个参数是
  `ToolTipIcon` 枚举，原来传 `1`，pythonnet 3 拒绝 int→enum 隐式转换，
  日志里一直是 `int can not be converted to Enum`。改成 `ToolTipIcon.Info`。

### Changed

- 调试开关 `HOMEWORK_DESK_AUTOSHOW_FLOAT=1` 改为走「关闭主窗口 → 吸进悬浮球」的
  完整路径（原先只是显示悬浮球），这样一条开关就能回归收缩动画和球的交互。

## [1.2.2] - 2026-10-28

### Fixed

- **主窗口标题栏拖不动**：原来靠 pywebview 注入的 JS 拖动（每次 mousemove 发一次
  IPC 调 `Window.move`，坐标用 `screenX - clientX` 推算），按住后窗口经常纹丝不动。
  改成 `DesktopBridge.window_drag()`：mousedown 时记下「光标 − 窗口原点」的偏移，
  之后按 60 fps 轮询全局光标、用 `SetWindowPos` 把窗口挪到「光标 − 偏移」，一次 IPC
  走完整个拖动；绝对位移，DPI 缩放下也不会累积误差。双击标题栏最大化保持不变。
- 顺手记下一条：`WM_NCLBUTTONDOWN + HTCAPTION` 在这类 frameless 窗口上不会启动系统
  移动循环（实测窗口完全不动），`ReleaseCapture()` 也只对本线程的鼠标捕获有效，
  所以那条老路走不通。

### Changed

- **收起主窗口改成「吸进悬浮球」**：不再只是页面淡出，而是先抓一帧主窗口画面，
  用分层窗口（`WS_EX_LAYERED` + `UpdateLayeredWindow`）按 `easeInQuad` 缩到悬浮球
  的位置并淡出，280 ms、60 fps；第一帧盖上去之后才隐藏真窗口，中间不会闪桌面。
  抓帧优先用 `PrintWindow(PW_RENDERFULLCONTENT)`（被遮挡或跑到屏幕外也能抓），
  失败再退回屏幕截图；都失败就直接切换，功能不受影响。新模块：`shrink_anim.py`。
- 两个模块都会声明 `gdi32.CreateDIBSection` / `user32.UpdateLayeredWindow` 的原型，
  而 `ctypes.windll` 是进程级共享的：结构体类型各写各的会互相覆盖，导致悬浮球
  创建绘图表面时直接 `TypeError`。现在统一声明成 `c_void_p`，调用处显式 `cast`。

## [1.2.1] - 2026-10-28

### Added

- 悬浮球**右键菜单**（原生分层窗口自绘）：右键球体原地展开成深色半透明菜单，
  菜单项为「打开主界面」「刷新数据」「隐藏悬浮球」「退出作业备忘」，退出项红色。
  再次右键 / Esc / 点菜单外面收起；菜单项集中在 `DesktopApplication._ball_menu()`，
  加项只需往列表里塞一个 dict。图标、悬停高亮、分隔线都是 GDI+ 画的，不依赖 WinForms。
- 悬浮球**左键展开概览卡片**：原地展开成 328 px 宽的深色毛玻璃卡片，显示今日到期、
  48 小时内紧急、已逾期三个数字，以及最紧急的 3 条作业（标题 / 课程 / 剩余时间）。
  点条目打开主界面，点 × 或卡片外面收起。
- 卡片与菜单共用同一套 `SetWindowPos` 缓动动画（约 0.24 秒展开 / 0.17 秒收起），
  展开时先长外形再淡入文字。
- **主窗口过渡动画**：显示主窗口时内容淡入 + 轻微上浮（280 ms），收起时反向淡出
  （180 ms）后再隐藏窗口，配合悬浮球出现，不再是生硬地“啪”一下消失 / 出现。
  动画由页面 CSS 完成（GPU 合成），Python 侧通过 `window.__appAnim('enter'|'leave')` 触发。

### Changed

- 悬浮球动画驱动改用**高精度可等待定时器**（`CreateWaitableTimerEx` +
  `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`）：原先 `SetTimer(16)` 被系统时钟节拍
  （15.625 ms）量化成 31 ms，动画只有 32 fps 且忽快忽慢；现在实测帧间隔中位数
  16.2 ms、无一帧超过 20 ms，稳定 60 fps。老系统上自动退回 `SetTimer(10)`。
- 展开 / 收起曲线分开：展开 easeOutCubic，收起改为加速吸入，手感更自然。
- 动画期间忽略 `WM_PAINT`（定时器已经在逐帧重画），每帧渲染次数从 1.1 降到 1.0。
- 悬浮球右键行为由「直接收起」改为「弹出操作菜单」；想收起球请用菜单里的「隐藏悬浮球」。
- 悬浮球的数据接口从 `(总数, 紧急, 逾期)` 三元组换成快照 dict（`_ball_snapshot()`），
  多带 `today` 和 `items`，供卡片使用。
- `BallWindow.show()` / `hide()` 改为向窗口线程投递命令（`WM_BALL_CMD`），
  不再从其它线程直接改窗口状态，避免和展开动画抢同一块 DIB。

### 发布

- 安装包：`..\version\作业备忘-安装程序-1.2.1.exe`（13.3 MB，Inno Setup，免管理员权限），
  同时复制到 `release\` 并重算了 `SHA256SUMS.txt`
  （`F0762E310492F91EA082A0D40339B38A7A60201F1370336F2FF42EF76DBC1967`）。
- 应用内版本号（`homework_server.APP_VERSION`）同步为 `1.2.1`。

## [1.2.0] - 2026-10-28

### Added

- 桌面悬浮球：关闭主窗口后在桌面出现 80×80 的透明圆球，置顶、不进任务栏、不抢焦点。
- 悬浮球用**原生分层窗口**（`ball_window.py`）实现：`WS_EX_LAYERED` + `UpdateLayeredWindow`
  + GDI+ 自绘圆球、阴影、图标与角标，圆外逐像素全透明（WebView2 在纯 HWND 宿主下做不到，
  会留一块方形底色）。
- 悬浮球带未完成作业数字角标（逾期/紧急时变红并呼吸），单击回到主界面，右键收起。
- 按住悬浮球可拖动，松手位置写入 `ball-state.json`，下次显示回到原处；拖出屏幕会自动夹回工作区。
- 关闭窗口后程序继续在后台运行，并在任务栏通知区域保留托盘图标。
- 托盘菜单：打开主界面、显示/收起悬浮球、退出作业备忘。
- 重复启动时不再弹“已经在运行”，而是把已有窗口重新显示出来。

### Changed

- 主窗口的 × / Alt+F4 改为「收起主界面 + 显示悬浮球」，不再是只收进托盘。
- 托盘菜单「显示悬浮窗 / 收起悬浮窗」文案改为「显示悬浮球 / 收起悬浮球」。
- 托盘右键菜单改为自绘样式：浅色圆角卡片、悬停墨绿胶囊高亮、矢量图标，退出项红色；单击图标直接打开主界面，「显示悬浮窗」按当前状态自动变成「收起悬浮窗」。
- 主窗口和悬浮窗改为无边框窗口，自绘 38px 标题栏（图标、标题、最小化 / 最大化 / 关闭）。
- 保留系统级四边缩放、Win11 圆角与描边；最大化时贴合工作区、不遮挡任务栏。
- 侧边滚动条改为隐形浮现：滚动时出现，停止约 1 秒后淡出，移到窗口右侧可重新显示并变墨绿。
- `floating.html`（380×500 旧版悬浮窗）不再被程序引用，保留在仓库里作为历史文件，也不打进安装包。
- `ball.html`（WebView2 版悬浮球页面）被 `ball_window.py` 取代，同样不再加载、不打进安装包，
  只保留作历史参考。
- 安装程序输出目录改为仓库外的 `..\version`，`ISCC.exe installer.iss` 直接产出到那里。

### 仓库 / 文档

- `release/` 换成 1.2.0 安装包，并重写 `SHA256SUMS.txt`（原来那份的中文文件名被写成了 `????`）。
- README 补上悬浮球预览图（`preview-ball.png`），主界面截图换成 1.2.0 的界面
  （按钮文案已是「悬浮球」），安装包路径改为 `release\`。
- HANDOVER 的发布流程、上传流程、故障排查都按原生悬浮球重写。

## [1.1.0] - 2026-10-26

### Added

- 独立的 Windows 桌面应用外壳（pywebview / WebView2），不再依赖浏览器页面。
- 约 380 × 500 的置顶悬浮窗，显示待办、紧急、逾期统计和紧凑作业列表。
- 主界面“悬浮窗 / 收起悬浮”切换按钮。
- 悬浮窗“完整界面”按钮；关闭悬浮窗标题栏也回到主界面。
- 主界面与悬浮窗之间的 BroadcastChannel 数据同步及窗口状态轮询。
- PyInstaller 打包配置、Inno Setup 安装程序。
- 桌面快捷方式、开始菜单快捷方式和开机启动项。
- 中文安装向导。

### Changed

- 本地服务改为由桌面程序内嵌启动，使用随机回环端口。
- 登录凭据和 Cookie 继续使用 Windows DPAPI 加密保存。
- 完成状态仍然只保存在本机，不向 LMS 提交。

### Verified

- 真实账号登录、自动登录、真实待办同步。
- 悬浮窗打开、切回完整界面、标题栏关闭和主程序退出。
- 安装目录在 `%LOCALAPPDATA%` 下时，普通用户权限可正常读写数据。

## [1.0.0] - 2026-10-26

### Added

- 深大优课待办同步与自动登录。
- 未完成作业过滤、截止时间升序、逾期置顶。
- 紧急 / 临近 / 正常三级高亮。
- 统计、手动刷新、最后更新时间、剩余时间倒计时。
- 本机完成标记。
- 本地单文件主界面原型。

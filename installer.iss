; 作业备忘 · Inno Setup 安装脚本
; 编译：ISCC.exe installer.iss

#define MyAppName "作业备忘"
#define MyAppVersion "1.4.3"
#define MyAppPublisher "个人项目"
#define MyAppExeName "HomeworkDesk.exe"
#define MyAppId "{{8F2B8F42-6F6D-4D7E-9B2A-2E5C5D1B7A31}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://lms.szu.edu.cn/user/index
DefaultDirName={localappdata}\Programs\HomeworkDesk
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\version
OutputBaseFilename=作业备忘-安装程序-1.4.3
SetupIconFile=homework-desk.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 界面是 WebView2 渲染的，Win11 及 Win10 1803+ 才自带；Win7/8/8.1 上 Edge/WebView2 已停止支持，
; 装上去也只会闪退，所以直接在安装阶段拦住（10.0.17763 = Windows 10 1809）。
MinVersion=10.0.17763
CloseApplications=yes
RestartApplications=no
AppMutex=Local\HomeworkDeskDesktopApp
VersionInfoVersion=1.4.3.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: checkedonce
Name: "startup"; Description: "开机自动启动（登录 Windows 后自动打开作业备忘）"; GroupDescription: "启动选项："; Flags: checkedonce

[Files]
Source: "dist\HomeworkDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动作业备忘"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

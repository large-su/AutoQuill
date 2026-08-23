; ============================================================
; AutoQuill 安装器（Inno Setup 6）
; 版本号由 tools/build_release.py 构建时从 core/version.py 自动注入，
; 请勿手工改这里的 MyAppVersion（改了也会被覆盖）。
;
; 设计原则：
;   - 用户目录安装（{localappdata}\Programs\AutoQuill），免管理员权限
;   - 卸载绝不删除 %APPDATA%\AutoQuill（用户数据：配置/API key/登录态/
;     采集素材/生成作品）
;   - 安装完成立即启动 AutoQuill.exe（一键启动器，自动拉起内置服务）
;
; 编译：ISCC installer\AutoQuill.iss
; ============================================================

#define MyAppName "AutoQuill"
#define MyAppVersion "4.5.0"
#define MyAppPublisher "AutoQuill"
#define MyAppExeName "AutoQuill.exe"

[Setup]
AppId={{F0D565E3-C1A9-40A6-894E-615014A3A357}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=AutoQuill-Setup-{#MyAppVersion}
WizardStyle=modern
SetupIconFile=..\assets\AutoQuill.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; 用户目录安装 + 未签名 exe：关闭安装器自身的权限/安全提示噪音
UninstallDisplayName={#MyAppName} {#MyAppVersion}

; 单语言（中文）：避免多语言导致安装器弹出「选择安装语言」对话框，
; 静默安装（/VERYSILENT）会因该对话框挂起
[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Files]
; 打包产物整体装入（onedir：exe + _internal/）
Source: "..\dist\AutoQuill\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; 图标单独装入：快捷方式显式指向 .ico 文件（覆盖安装时 Shell 从
; exe 提取图标会命中旧缓存显示空白/旧图标，V4.2.1 用户反馈）
Source: "..\assets\AutoQuill.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\AutoQuill.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\AutoQuill.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

; 用户数据在 %APPDATA%\AutoQuill（DATA_ROOT），与安装目录分离：
; 卸载（删除 {app}）不会触碰用户数据。数据目录的清理说明见 README
; 的「卸载」一节——本安装器刻意不做任何 [UninstallDelete] 操作。

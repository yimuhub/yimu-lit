; 一幕·YIMU 神笔 Windows 安装包（Inno Setup 6）
; 前置：先跑 build_exe.ps1 产出 dist\YiMuLit\（PyInstaller onedir）
#define AppName "一幕神笔"
#define AppExe "YiMuLit.exe"

[Setup]
AppId={{8C1F2A34-9D6E-4E52-9A2B-YIMULIT2026}}
AppName={#AppName}
AppVersion=4.28.0
AppPublisher=YiMuHub
DefaultDirName={autopf}\YiMuLit
DefaultGroupName={#AppName}
OutputBaseFilename=一幕神笔-4.28.0-windows-setup
OutputDir=..\..\dist
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "..\..\dist\YiMuLit\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载只删程序文件；用户数据在 %APPDATA%\YiMuLit，保留
; 如需连数据一起删，取消下行注释：
; Type: filesandordirs; Name: "{userappdata}\YiMuLit"

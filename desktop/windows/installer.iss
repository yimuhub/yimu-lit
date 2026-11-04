; 一幕·YIMU 神笔 Windows 安装包（Inno Setup 6；本文件必须保持 UTF-8 with BOM 编码）
; 前置：先跑 desktop\windows\build_exe.ps1 产出 dist\YiMuLit\（PyInstaller onedir）
#define AppName "一幕神笔"
#define AppExe "YiMuLit.exe"
; AppVersion 由构建脚本注入（读 VERSION），此为兜底
#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A1C2B38-4D5E-4F60-8192-A3B4C5D6E7F8}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=YiMuHub
DefaultDirName={autopf}\YiMuLit
DefaultGroupName={#AppName}
OutputBaseFilename=YiMuLit-{#AppVersion}-windows-setup
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
; Type: filesandordirs; Name: "{userappdata}\YiMuLit"

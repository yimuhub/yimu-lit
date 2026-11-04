# 一幕·YIMU 神笔 Windows 构建脚本（在 Windows 上执行）
# 用法：powershell -ExecutionPolicy Bypass -File desktop\windows\build_exe.ps1
# 前置：Python 3.12+（已加入 PATH）；Inno Setup 6（ISCC.exe 在 PATH 或默认安装位置）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

Write-Host "== 1/4 虚拟环境 ==" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
& .venv\Scripts\python.exe -m pip install -q --upgrade pip
& .venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller

Write-Host "== 2/4 PyInstaller 打包 ==" -ForegroundColor Cyan
& .venv\Scripts\pyinstaller.exe desktop\yimulit.spec --noconfirm --distpath desktop\dist --workpath desktop\build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

Write-Host "== 3/4 冒烟测试 ==" -ForegroundColor Cyan
$env:YIMU_DATA_DIR = Join-Path $env:TEMP ("yimulit_smoke_" + [guid]::NewGuid())
$env:YIMU_PORT = "46995"
$proc = Start-Process -FilePath "desktop\dist\YiMuLit\YiMuLit.exe" -PassThru
Start-Sleep -Seconds 12
try {
  $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:46995/" -TimeoutSec 10
  if ($r.StatusCode -ne 200) { throw "首页状态码 $($r.StatusCode)" }
  Write-Host "冒烟通过：首页 200" -ForegroundColor Green
} finally { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }

Write-Host "== 4/4 Inno Setup 安装包 ==" -ForegroundColor Cyan
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "ISCC" }
& $iscc desktop\windows\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失败" }

Write-Host "OK: desktop\dist\ 一幕神笔-4.28.0-windows-setup.exe" -ForegroundColor Green

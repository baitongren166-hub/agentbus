# 在 Windows 上一键打包 agentbus 安装包（右键「用 PowerShell 运行」，或在 PowerShell 里 .\scripts\build-windows.ps1）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
Write-Host "== 检查工具 ==" -ForegroundColor Cyan
$missing = @()
foreach ($t in "python", "node", "npm", "cargo") { if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { $missing += $t } }
if ($missing.Count) { Write-Host "缺少：$($missing -join '、')。按《Windows打包说明.md》第 1 步装好后重开 PowerShell 再运行。" -ForegroundColor Red; exit 1 }
Write-Host "== 1/3 打包本地服务（Python → agentbus-core.exe）==" -ForegroundColor Cyan
python -m pip install --upgrade pyinstaller
python scripts\build-core.py
Write-Host "== 2/3 安装应用依赖 ==" -ForegroundColor Cyan
Set-Location app
npm ci
Write-Host "== 3/3 打包应用（第一次要编译十几分钟）==" -ForegroundColor Cyan
npx tauri build
$exe = Get-ChildItem "src-tauri\target\release\bundle\nsis\*.exe" | Select-Object -First 1
Write-Host "好了，安装包在：$($exe.FullName)" -ForegroundColor Green
explorer.exe /select,"$($exe.FullName)"

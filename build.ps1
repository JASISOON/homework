#requires -Version 5.1
$ErrorActionPreference = 'Stop'

$py = Join-Path $env:WINDIR 'py.exe'
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'

if (-not (Test-Path $py)) {
    throw '找不到 Python Launcher：C:\Windows\py.exe。请安装 Python 3.14。'
}

Write-Host '构建 HomeworkDesk.exe ...' -ForegroundColor Green
& $py -3.14 -m PyInstaller --noconfirm --clean HomeworkDesk.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }

Write-Host '构建安装程序 ...' -ForegroundColor Green
if (-not (Test-Path $iscc)) {
    throw "找不到 Inno Setup：$iscc"
}
& $iscc .\installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 构建失败。' }

Write-Host ''
Write-Host '构建完成：' -ForegroundColor Green
Get-ChildItem .\dist_installer\*.exe | Select-Object Name, Length, LastWriteTime

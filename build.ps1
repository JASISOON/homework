#requires -Version 5.1
$ErrorActionPreference = 'Stop'

$py = Join-Path $env:WINDIR 'py.exe'
$isccCandidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not (Test-Path $py)) {
    throw '找不到 Python Launcher：C:\Windows\py.exe。请安装 Python 3.14。'
}

Write-Host '构建 HomeworkDesk.exe ...' -ForegroundColor Green
& $py -3.14 -m PyInstaller --noconfirm --clean HomeworkDesk.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }

Write-Host '构建安装程序 ...' -ForegroundColor Green
if (-not $iscc) {
    throw '找不到 Inno Setup 6（ISCC.exe）。请先安装：winget install JRSoftware.InnoSetup'
}
& $iscc .\installer.iss
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 构建失败。' }

Write-Host ''
Write-Host '构建完成：' -ForegroundColor Green
Get-ChildItem (Join-Path $PSScriptRoot '..\version\*.exe') | Select-Object Name, Length, LastWriteTime

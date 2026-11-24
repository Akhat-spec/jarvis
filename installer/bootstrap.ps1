# Установщик J.A.R.V.I.S.: находит Python 3.12+ (или предлагает поставить его), затем запускает мастер установки.
param([string]$App = (Join-Path $PSScriptRoot 'app'))
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Windows.Forms
$Title = 'Установка J.A.R.V.I.S.'

function Show-Msg($text, $buttons = 'OK', $icon = 'Information') {
    [System.Windows.Forms.MessageBox]::Show($text, $Title, $buttons, $icon)
}

function Test-Python($exe) {
    # Python 3.12+ с tkinter и venv; возвращает путь к python.exe или $null
    try {
        $out = & $exe -c "import sys, tkinter, venv; print(sys.executable if sys.version_info >= (3, 12) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    } catch {}
    return $null
}

function Find-Python {
    # 1) py — лаунчер python.org и Python Install Manager: сам выбирает свежую версию
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $exe = Test-Python $py.Source; if ($exe) { return $exe } }
    # 2) установки python.org и Python Install Manager — свежие версии первыми
    $cands = @()
    foreach ($base in @("$env:LOCALAPPDATA\Python", "$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*")) {
        $cands += Get-ChildItem $base -Filter python.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    }
    # 3) python из PATH; заглушку WindowsApps берём, только если Python из Microsoft Store реально установлен
    foreach ($c in Get-Command python.exe -All -ErrorAction SilentlyContinue) {
        if ($c.Source -notlike '*\WindowsApps\*' -or
            (Get-AppxPackage -Name 'PythonSoftwareFoundation.Python.3*' -ErrorAction SilentlyContinue)) { $cands += $c.Source }
    }
    foreach ($c in ($cands | Select-Object -Unique)) {
        $exe = Test-Python $c
        if ($exe) { return $exe }
    }
    return $null
}

if (-not (Test-Path (Join-Path $App 'setup_wizard.py'))) {
    Show-Msg "Не найдены файлы установки рядом с установщиком:`n$App" 'OK' 'Error' | Out-Null
    exit 1
}

$python = Find-Python
if (-not $python) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        $ans = Show-Msg ("Для работы Джарвиса нужен Python (бесплатно, ~30 МБ).`n`n" +
            "Установить Python 3.13 автоматически? Установка пойдёт через winget от Microsoft, " +
            "только для вашей учётной записи, без прав администратора.") 'YesNo' 'Question'
        if ($ans -ne 'Yes') { exit 1 }
        Start-Process $winget.Source -Wait -ArgumentList @('install', '-e', '--id', 'Python.Python.3.13',
            '--scope', 'user', '--accept-package-agreements', '--accept-source-agreements')
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $python = Find-Python
    }
    if (-not $python) {
        Show-Msg ("Не удалось найти или установить Python.`n`n" +
            "Сейчас откроется python.org: скачайте Python 3.13, при установке отметьте «Add python.exe to PATH», " +
            "затем снова запустите установщик Джарвиса.") 'OK' 'Warning' | Out-Null
        Start-Process 'https://www.python.org/downloads/windows/'
        exit 1
    }
}

$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$p = Start-Process $pythonw -ArgumentList ('"' + (Join-Path $App 'setup_wizard.py') + '"') -WorkingDirectory $App -PassThru -Wait
exit $p.ExitCode

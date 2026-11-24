# Автозапуск J.A.R.V.I.S. при входе в Windows.
#   Включить:  powershell -ExecutionPolicy Bypass -File autostart.ps1
#   Выключить: powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
param([switch]$Remove)

$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'J.A.R.V.I.S.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Output "Автозапуск выключен." }
    else { Write-Output "Автозапуск и так не был включён." }
    return
}

# Настоящий pythonw.exe рядом с python.exe (псевдонимы из WindowsApps при автозапуске ненадёжны)
$pythonw = & python -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) { Write-Error "Не найден pythonw.exe — проверьте установку Python."; return }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = '"' + (Join-Path $PSScriptRoot 'run.py') + '"'
$sc.WorkingDirectory = $PSScriptRoot
$sc.Description = 'J.A.R.V.I.S. — голосовой ассистент'
$sc.Save()
Write-Output "Автозапуск включён: $link"

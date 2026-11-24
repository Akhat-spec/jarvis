# Автозапуск LibreHardwareMonitor с правами администратора при входе в Windows (нужно для температуры CPU).
#   Включить:  powershell -ExecutionPolicy Bypass -File lhm_autostart.ps1      (запросит права администратора)
#   Выключить: powershell -ExecutionPolicy Bypass -File lhm_autostart.ps1 -Remove
param([switch]$Remove)

$name = "LibreHardwareMonitor (Jarvis)"
$exe = Join-Path $PSScriptRoot "LibreHardwareMonitor\LibreHardwareMonitor.exe"
$log = Join-Path $PSScriptRoot "lhm_autostart.log"

function Write-Log($text) { "$(Get-Date -Format 'dd.MM.yyyy HH:mm:ss')  $text" | Out-File $log -Append -Encoding utf8 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Log "запрашиваю права администратора"
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Remove) { $argList += " -Remove" }
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait
    } catch {
        Write-Log "права администратора не получены: $($_.Exception.Message)"
        Write-Output "Права администратора не получены — задача не создана."
    }
    return
}

try {
    if ($Remove) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force
        Write-Log "автозапуск выключен"
        return
    }
    if (-not (Test-Path $exe)) { throw "не найден $exe" }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $name
    Write-Log "автозапуск включён, программа запущена"
} catch {
    Write-Log "ошибка: $($_.Exception.Message)"
}

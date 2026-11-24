"""Правила безопасности: насколько рискованно действие.

Уровни риска:
  safe      — только чтение (время, список окон, чтение файла): выполняется всегда;
  normal    — обычные действия (открыть сайт, переключить окно, напечатать текст);
  dangerous — может навредить или что-то отправить (удаление, команды, ввод в банк/мессенджер/терминал);
  blocked   — запрещено всегда, даже с подтверждением.

Уровни автономности (переключатель в HUD):
  chat    — только safe; всё остальное отклоняется;
  confirm — safe сразу, normal и dangerous после «да»;
  auto    — safe и normal сразу, dangerous после «да».
"""
import re

RISK_ORDER = {"safe": 0, "normal": 1, "dangerous": 2, "blocked": 3}

# Окна, где клик или ввод могут привести к оплате, отправке сообщения, смене пароля или системных настроек
CRITICAL_WINDOW_WORDS = (
    "банк", "bank", "kaspi", "halyk", "jusan", "freedom", "сбер", "тинькофф", "т-банк", "paypal", "stripe",
    "оплат", "payment", "checkout", "корзин", "кошел", "wallet", "binance", "crypto", "крипт",
    "пароль", "password", "1password", "bitwarden", "keepass", "lastpass", "authenticator",
    "telegram", "whatsapp", "discord", "slack", "viber", "messenger", "outlook", "gmail", "почта", "mail",
    "безопасность windows", "windows security", "контроль учетных записей", "user account control",
    "диспетчер устройств", "редактор реестра", "registry editor", "параметры", "settings",
)
CRITICAL_PROCESSES = (
    "consent.exe", "regedit.exe", "mmc.exe", "taskmgr.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "wt.exe", "conhost.exe", "telegram.exe", "whatsapp.exe", "discord.exe",
    "slack.exe", "outlook.exe", "keepass.exe", "1password.exe", "bitwarden.exe", "securityhealthsystray.exe",
)


def critical_window_reason(win: dict | None) -> str | None:
    if not win:
        return "не удалось определить окно под курсором"
    title, proc = win.get("title", "").lower(), win.get("process", "").lower()
    if proc in CRITICAL_PROCESSES:
        return f"критичное приложение ({win.get('process')})"
    for w in CRITICAL_WINDOW_WORDS:
        if w in title:
            return f"критичное окно («{win.get('title')}»)"
    return None


# Опасные сочетания клавиш
DANGEROUS_HOTKEYS = {"alt+f4", "shift+delete", "ctrl+shift+delete", "win+l", "win+r", "ctrl+alt+delete",
                     "ctrl+shift+esc", "win+x"}

# ───────── команды PowerShell ─────────
_BLOCKED_PATTERNS = [
    r"(^|[\s;|&(])format(\.com)?\s+[a-z]:", r"\bformat-volume\b", r"\bdiskpart\b", r"\bbcdedit\b", r"\bvssadmin\b.*\bdelete\b", r"\bcipher\b.*\/w",
    r"\bclear-disk\b", r"\binitialize-disk\b", r"\bremove-partition\b",
    r"set-mppreference", r"add-mppreference", r"disable-?windowsoptionalfeature.*defender",
    r"set-executionpolicy", r"-enc(odedcommand)?\b", r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)",
    r"(remove-item|del|rd|rmdir|rm)\b.*\b(c:\\windows|c:\\program files|\$env:windir|system32)",
    r"\bnet\s+user\b.*\/(add|delete)", r"\btakeown\b", r"\bicacls\b.*\/grant",
    # скачать из интернета и сразу выполнить (в любых сокращениях)
    r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|downloadstring|downloadfile)\b.*\|\s*(iex|invoke-expression)\b",
    r"\b(iex|invoke-expression)\b.*\b(downloadstring|irm|iwr|invoke-webrequest|invoke-restmethod|net\.webclient)\b",
    r"\bstart-bitstransfer\b.*\.(exe|ps1|bat|cmd|vbs|msi)\b",
]
_READONLY_COMMANDS = {
    "get-childitem", "dir", "ls", "gci", "get-item", "get-content", "cat", "type", "gc", "get-process", "ps",
    "tasklist", "get-date", "date", "ipconfig", "systeminfo", "whoami", "hostname", "get-location", "pwd",
    "test-path", "resolve-path", "get-volume", "get-psdrive", "get-service", "get-computerinfo",
    "get-netadapter", "get-netipaddress", "ping", "test-connection", "where", "where.exe", "echo",
    "write-output", "measure-object", "select-object", "sort-object", "where-object", "format-table",
    "format-list", "select-string", "get-command", "get-help", "nslookup", "get-ciminstance", "get-wmiobject",
    "get-itemproperty", "get-acl", "get-filehash", "get-uptime", "out-string", "group-object",
}


def classify_command(cmd: str) -> tuple[str, str]:
    low = cmd.lower()
    for p in _BLOCKED_PATTERNS:
        if re.search(p, low):
            return "blocked", "команда может повредить систему или отключить защиту"
    # подвыражения $(…), @(…), вызов через & и обратные кавычки могут спрятать любую команду
    # внутри безобидной (echo $(Remove-Item …)) — такие команды считаем изменяющими
    if re.search(r"\$\(|@\(|`|(^|[\s;|(])&\s*\S|\b(invoke-expression|iex|start-process|saps|start)\b", low):
        return "dangerous", "команда может что-то изменить в системе"
    # убираем {…}-блоки (вычисляемые поля, фильтры) и строки, затем делим по ; | && и смотрим первые слова
    shape = re.sub(r"'[^']*'|\"[^\"]*\"", "''", low)
    had_blocks = False
    while re.search(r"\{[^{}]*\}", shape):
        shape = re.sub(r"\{[^{}]*\}", "", shape)
        had_blocks = True
    if had_blocks and re.search(r"\b(remove|set|new|stop|start|invoke|move|rename|copy|clear|out)-|>", low):
        return "dangerous", "команда может что-то изменить в системе"
    parts = [p.strip() for p in re.split(r"[;|&]+", shape) if p.strip()]
    words = [p.split()[0] for p in parts if p.split()]
    if words and all(w in _READONLY_COMMANDS for w in words) and ">" not in low:
        return "normal", "команда только читает данные"
    return "dangerous", "команда может что-то изменить в системе"


def needs_confirmation(risk: str, autonomy: str) -> bool | None:
    """True — спросить, False — выполнить, None — запрещено в этом режиме."""
    if risk == "blocked":
        return None
    if autonomy == "chat":
        return None if risk != "safe" else False
    if autonomy == "auto":
        return risk == "dangerous"
    return risk != "safe"  # confirm (по умолчанию)

"""Сборка установщика: dist/JARVIS-Setup.exe (один файл) и dist/JARVIS-Setup.zip (то же без .exe).

    python installer/build.py

Личные данные (ключи .env, вход Google, память, настройки, журналы) в сборку не попадают.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from setup_wizard import SKIP  # noqa: E402

DIST = ROOT / "dist"
STAGE = DIST / "build"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
EXTRA_SKIP = {"installer", "dist", "README.md"}
SECRETS = {".env", ".ws_token", "google_token.json", "google_client_secret.json", "memory.json", "hud_settings.json"}

LHM_NOTE = """LibreHardwareMonitor — сторонняя программа для чтения датчиков (температура процессора).
Лицензия: Mozilla Public License 2.0 — https://www.mozilla.org/MPL/2.0/
Исходный код и официальные сборки: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
"""


# чистые настройки LibreHardwareMonitor: свёрнут в трей, веб-сервер датчиков на 8085 (без данных этого компьютера)
LHM_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerIp" value="127.0.0.1" />
    <add key="listenerPort" value="8085" />
  </appSettings>
</configuration>
"""


def ignore(_dir, names):
    return [n for n in names if n in SKIP or n in EXTRA_SKIP or n.endswith((".pyc", ".log"))]


def main():
    shutil.rmtree(STAGE, ignore_errors=True)
    app = STAGE / "app"
    shutil.copytree(ROOT, app, ignore=ignore)
    (app / "tools" / "LibreHardwareMonitor" / "LICENSE-NOTE.txt").write_text(LHM_NOTE, encoding="utf-8")
    (app / "tools" / "LibreHardwareMonitor" / "LibreHardwareMonitor.config").write_text(LHM_CONFIG, encoding="utf-8")
    shutil.copy2(HERE / "bootstrap.ps1", STAGE / "bootstrap.ps1")

    leaked = [p for p in STAGE.rglob("*") if p.name in SECRETS]
    if leaked:
        sys.exit(f"В сборку попали личные файлы: {leaked}")

    payload = DIST / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(STAGE.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(STAGE).as_posix())

    # zip для тех, кому .exe не скачивается: распаковать и запустить «Установить.cmd»
    portable = DIST / "JARVIS-Setup.zip"
    shutil.copy2(payload, portable)
    with zipfile.ZipFile(portable, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Установить.cmd", "@echo off\r\nstart \"\" powershell -NoProfile -ExecutionPolicy Bypass "
                                     "-WindowStyle Hidden -File \"%~dp0bootstrap.ps1\"\r\n")

    exe = DIST / "JARVIS-Setup.exe"
    cmd = [str(CSC), "/nologo", "/target:winexe", "/optimize+", "/codepage:65001", f"/out:{exe}",
           f"/win32icon:{ROOT / 'jarvis.ico'}", f"/win32manifest:{HERE / 'Setup.manifest'}",
           f"/resource:{payload},payload.zip", "/r:System.IO.Compression.dll",
           "/r:System.IO.Compression.FileSystem.dll", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", str(HERE / "Setup.cs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="cp866", errors="replace")
    if r.returncode != 0:
        sys.exit("csc: " + r.stdout + r.stderr)
    payload.unlink()
    shutil.rmtree(STAGE, ignore_errors=True)
    for f in (exe, portable):
        print(f"{f.name}: {f.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()

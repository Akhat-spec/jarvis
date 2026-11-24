"""Выкладывает исходники в репозиторий GitHub одним коммитом (без git, через Git Data API).

    python installer/publish_github.py OWNER/REPO "сообщение коммита"

Токен берётся из `gh auth token`. Личные файлы (ключи, вход Google, память, настройки, журналы) не выкладываются.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from setup_wizard import SKIP  # noqa: E402
from installer.build import LHM_CONFIG, LHM_NOTE  # noqa: E402

GH = Path.home() / r"AppData\Local\Microsoft\WinGet\Packages\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe"
EXCLUDE = (SKIP - {"tests", "Jarvis.bat", "autostart.ps1", "jarvis.py"}) | {
    "dist", "README.md", "__pycache__", "jarvis.py", "Включить температуру CPU.bat", "README.github.md"}
SECRETS = {".env", ".ws_token", "google_token.json", "google_client_secret.json", "memory.json", "hud_settings.json"}
GITIGNORE = """# личные данные — никогда не коммитить
.env
.ws_token
google_token.json
google_client_secret.json
memory.json
hud_settings.json
wallpaper.json
logs/
.hud_profile/
.wall_profile/
hud/tile_img/
tools/we_backup/
tools/*.log
# сборка и окружение
.venv/
dist/
__pycache__/
*.pyc
"""


def files():
    out = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if not p.is_file() or any(part in EXCLUDE for part in rel.parts) or p.suffix in (".pyc", ".log"):
            continue
        if p.name in SECRETS:
            sys.exit(f"личный файл в списке: {rel}")
        data = LHM_CONFIG.encode() if p.name == "LibreHardwareMonitor.config" else p.read_bytes()
        out.append((rel.as_posix(), data))
    out.append(("tools/LibreHardwareMonitor/LICENSE-NOTE.txt", LHM_NOTE.encode()))
    out.append(("README.md", (HERE / "README.github.md").read_bytes()))
    out.append((".gitignore", GITIGNORE.encode()))
    return out


def main():
    repo, message = sys.argv[1], sys.argv[2]
    token = subprocess.run([str(GH), "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()

    def api(method, path, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jarvis-publish"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')[:500]}")

    head = api("GET", "/git/ref/heads/main")["object"]["sha"]
    tree = []
    items = files()
    for i, (path, data) in enumerate(items, 1):
        blob = api("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"\r{i}/{len(items)}", end="", flush=True)
    t = api("POST", "/git/trees", {"tree": tree})
    c = api("POST", "/git/commits", {"message": message, "tree": t["sha"], "parents": [head]})
    api("PATCH", "/git/refs/heads/main", {"sha": c["sha"]})
    print(f"\n{len(items)} файлов, коммит {c['sha'][:7]}")


if __name__ == "__main__":
    main()

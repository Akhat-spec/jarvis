"""Системные показатели для HUD: загрузка CPU/GPU/ОЗУ, скорость диска, температуры.

Основной источник температур и GPU — веб-сервер LibreHardwareMonitor (один HTTP-запрос в секунду,
tools\\LibreHardwareMonitor). Если он не запущен — запасные: nvidia-smi для видеокарты и ACPI-зона
через PowerShell для CPU (их запуск дороже, поэтому реже). Только настоящие данные: нет источника —
None, и HUD показывает прочерк.
"""
import json
import subprocess
import threading
import time
import urllib.request

import psutil

NO_WINDOW = 0x08000000
LHM_URL = "http://127.0.0.1:8085/data.json"
_CPU_NAMES = ("cpu package", "core (tctl/tdie)", "core (tctl)", "core max", "core average")


def _num(value) -> float | None:
    try:
        return float(str(value).split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None


def lhm_read() -> dict | None:
    """Температура CPU, загрузка и температура дискретной видеокарты из LibreHardwareMonitor."""
    try:
        with urllib.request.urlopen(LHM_URL, timeout=2) as r:
            tree = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    cpu, gpu_load, gpu_temp = {}, None, None

    def walk(node):
        nonlocal gpu_load, gpu_temp
        for c in node.get("Children", []):
            sid, text = str(c.get("SensorId", "")), str(c.get("Text", "")).lower()
            if "cpu" in sid and "/temperature/" in sid:
                cpu.setdefault(text, c.get("Value"))
            elif sid.startswith(("/gpu-nvidia", "/gpu-amd")) and text == "gpu core":
                if "/load/" in sid and gpu_load is None:
                    gpu_load = _num(c.get("Value"))
                elif "/temperature/" in sid and gpu_temp is None:
                    gpu_temp = _num(c.get("Value"))
            walk(c)

    walk(tree)
    cpu_t = next((_num(cpu[n]) for n in _CPU_NAMES if n in cpu), None)
    if cpu_t is not None and not 5 < cpu_t < 120:
        cpu_t = None
    return {"cpuT": cpu_t, "gpu": gpu_load, "gpuT": gpu_temp}


def _nvidia_smi() -> tuple[float | None, float | None]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=3, creationflags=NO_WINDOW).stdout.strip().splitlines()
        util, temp = (float(x) for x in out[0].split(",")[:2])
        return util, temp
    except Exception:
        return None, None


_PS_ACPI = ("$z = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
            "-ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty CurrentTemperature; "
            "if ($z) { [math]::Round($z / 10 - 273.15, 1) }")


def _acpi_cpu_temp() -> float | None:
    """Запасной вариант: ACPI-зона (обычно нужны права администратора)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_ACPI], capture_output=True,
                             text=True, timeout=8, creationflags=NO_WINDOW).stdout.strip()
        v = _num(out)
        return v if v is not None and 5 < v < 120 else None
    except Exception:
        return None


def _cpu_temp() -> float | None:
    """Для обратной совместимости: температура CPU из любого доступного источника."""
    lhm = lhm_read()
    return lhm["cpuT"] if lhm and lhm["cpuT"] is not None else _acpi_cpu_temp()


class SysMonitor:
    """Опрашивает показатели раз в секунду, пока кто-то смотрит (active() возвращает True)."""

    def __init__(self, emit, active):
        self.emit, self.active = emit, active
        self.last: dict | None = None
        threading.Thread(target=self._loop, daemon=True, name="sysinfo").start()

    def _loop(self):
        psutil.cpu_percent(None)
        io, t_io = psutil.disk_io_counters(), time.time()
        smi = (None, None)
        acpi = None
        tick = 0
        while True:
            time.sleep(1)
            if not self.active():
                psutil.cpu_percent(None)
                io, t_io = psutil.disk_io_counters(), time.time()
                continue
            tick += 1
            now = time.time()
            io2 = psutil.disk_io_counters()
            disk = ((io2.read_bytes + io2.write_bytes) - (io.read_bytes + io.write_bytes)) / max(0.2, now - t_io) / 2**20
            io, t_io = io2, now

            lhm = lhm_read() or {}
            gpu, gpu_t = lhm.get("gpu"), lhm.get("gpuT")
            if gpu is None or gpu_t is None:      # нет LibreHardwareMonitor — nvidia-smi раз в 2 с
                if tick % 2 == 1:
                    smi = _nvidia_smi()
                gpu = gpu if gpu is not None else smi[0]
                gpu_t = gpu_t if gpu_t is not None else smi[1]
            cpu_t = lhm.get("cpuT")
            if cpu_t is None:                     # и ACPI через PowerShell — раз в минуту
                if tick % 60 == 1:
                    acpi = _acpi_cpu_temp()
                cpu_t = acpi

            self.last = {
                "type": "sys",
                "cpu": round(psutil.cpu_percent(None), 1),
                "ram": round(psutil.virtual_memory().percent, 1),
                "disk": round(disk, 1),
                "gpu": gpu, "gpuT": gpu_t, "cpuT": cpu_t,
            }
            self.emit(self.last)

"""子进程管理：ComfyUI 引擎与绘画模块。

所有子进程挂在同一个 Windows Job Object 上（KILL_ON_JOB_CLOSE），
前端服务进程结束（含直接关闭控制台窗口）时由系统一并结束子进程。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import httpx
import psutil

from . import config

log = logging.getLogger("frontend.procs")

# ───────────────────────────── Job Object ─────────────────────────────

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Job:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        self._k32 = k32
        h = k32.CreateJobObjectW(None, None)
        if not h:
            log.warning("CreateJobObject 失败，关闭窗口时子进程可能残留")
            return
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(wintypes.HANDLE(h), _JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("SetInformationJobObject 失败")
            return
        self.handle = h

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        ph = self._k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not ph:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(wintypes.HANDLE(self.handle), wintypes.HANDLE(ph)))
        finally:
            self._k32.CloseHandle(wintypes.HANDLE(ph))


JOB = _Job()
# 只有正式运行（__main__）时才把已在运行的外部进程纳入 Job Object；
# 否则任何导入本模块的脚本退出时都会连带结束 ComfyUI / 绘画模块。
ADOPT_INTO_JOB = False

# ───────────────────────────── 服务 ─────────────────────────────


def _listening_pid(port: int) -> int | None:
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except (psutil.Error, OSError):
        pass
    return None


def _kill_tree(pid: int, timeout: float = 10.0) -> None:
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = [p] + p.children(recursive=True)
    for q in procs:
        try:
            q.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for q in alive:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass


class Service:
    """一个受管理的子进程服务。状态：stopped / starting / running / unresponsive / stopping。"""

    def __init__(self, name: str, label: str, cmd: list[str], cwd: Path, port: int, health_url: str,
                 log_path: Path, start_timeout: float, env_fn=None):
        self.name, self.label = name, label
        self.env_fn = env_fn           # 启动时追加的环境变量（如绘画模块的输出目录）
        self.applied_env: dict | None = None   # 本次进程启动时实际使用的追加变量；接管的外部进程为 None
        self.cmd, self.cwd, self.port = cmd, cwd, port
        self.health_url, self.log_path = health_url, log_path
        self.start_timeout = start_timeout
        self.pid: int | None = None
        self.external = False          # 前端启动前就已在运行、被接管的进程
        self.started_at: float | None = None
        self.state = "stopped"
        self.error: str | None = None
        self._popen: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._op = 0                   # 每次启动/停止递增，旧的就绪监视线程据此退出

    # ── 查询 ──
    def healthy(self, timeout: float = 2.0) -> bool:
        try:
            return httpx.get(self.health_url, timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    def _alive(self) -> bool:
        if self._popen is not None:
            return self._popen.poll() is None
        if self.pid:
            return psutil.pid_exists(self.pid)
        return False

    def refresh(self) -> str:
        with self._lock:
            if self.state in ("starting", "stopping"):
                return self.state
            if self._alive():
                self.state = "running" if self.healthy() else "unresponsive"
            else:
                pid = _listening_pid(self.port)
                if pid and self.healthy():
                    self._adopt(pid)
                    self.state = "running"
                else:
                    self.pid, self._popen, self.state = None, None, "stopped"
            return self.state

    def log_tail(self, lines: int = 40) -> str:
        try:
            data = self.log_path.read_bytes()[-20000:].decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(data.splitlines()[-lines:])

    def info(self) -> dict:
        state = self.refresh()
        return {"name": self.name, "label": self.label, "state": state, "pid": self.pid,
                "external": self.external, "port": self.port, "error": self.error,
                "uptime": (time.time() - self.started_at) if (self.started_at and state == "running") else None}

    # ── 控制 ──
    def _adopt(self, pid: int) -> None:
        self.pid, self._popen, self.external = pid, None, True
        self.applied_env = None
        self.started_at = self.started_at or time.time()
        if ADOPT_INTO_JOB and JOB.assign(pid):
            log.info("%s：接管已在运行的进程 PID %s（已纳入随窗口关闭）", self.label, pid)
        else:
            log.info("%s：接管已在运行的进程 PID %s", self.label, pid)

    def start(self, attempt: int = 1) -> None:
        with self._lock:
            if self.state in ("starting", "running"):
                return
            pid = _listening_pid(self.port)
            if pid:
                if self.healthy():
                    self._adopt(pid)
                    self.state, self.error = "running", None
                    return
                _kill_tree(pid)   # 端口被无响应的旧进程占用
            self._op += 1
            op = self._op
            self.error, self.external = None, False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(self.log_path, "ab")
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {self.label} =====\n".encode("utf-8"))
            logf.flush()
            extra = self.env_fn() if self.env_fn else {}
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **extra)
            self.applied_env = extra
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            self._popen = subprocess.Popen(self.cmd, cwd=str(self.cwd), stdout=logf, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=env, creationflags=flags)
            logf.close()
            self.pid = self._popen.pid
            JOB.assign(self.pid)
            self.started_at = time.time()
            self.state = "starting"
            log.info("%s：启动中（PID %s）", self.label, self.pid)
        threading.Thread(target=self._watch_ready, args=(op, attempt), daemon=True).start()

    def _bind_failed(self) -> bool:
        tail = self.log_tail(15)
        return "error while attempting to bind" in tail or "10013" in tail or "10048" in tail

    def _watch_ready(self, op: int, attempt: int = 1) -> None:
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if op != self._op:
                return
            if self._popen is not None and self._popen.poll() is not None:
                bind = self._bind_failed()
                with self._lock:
                    if op != self._op:
                        return
                    self.state = "stopped"
                    if bind:
                        self.error = (f"端口 {self.port} 无法绑定（可能被 WSL / Hyper-V / Docker 临时保留，或被其它程序占用）。"
                                      "可以管理员身份运行根目录的 reset_ports.bat 重置端口后，在 菜单 → 引擎 中点「启动」重试")
                    else:
                        self.error = f"进程已退出（退出码 {self._popen.returncode}），详见日志"
                if attempt < 3:
                    log.warning("%s：%s；%d 秒后自动重试（第 %d 次）", self.label, self.error, 3 * attempt, attempt)
                    time.sleep(3 * attempt)
                    if op == self._op:
                        self.start(attempt + 1)
                else:
                    log.error("%s：%s", self.label, self.error)
                return
            if self.healthy():
                with self._lock:
                    if op == self._op:
                        self.state = "running"
                        log.info("%s：已就绪（%.0f 秒）", self.label, time.time() - self.started_at)
                return
            time.sleep(1.0)
        with self._lock:
            if op == self._op:
                self.state = "unresponsive"
                self.error = f"{int(self.start_timeout)} 秒内未就绪"
                log.error("%s：%s", self.label, self.error)

    def stop(self) -> None:
        with self._lock:
            self._op += 1
            pid = self.pid or _listening_pid(self.port)
            if not pid:
                self.state = "stopped"
                return
            self.state = "stopping"
        log.info("%s：停止中", self.label)
        _kill_tree(pid)
        with self._lock:
            self.pid, self._popen, self.external, self.started_at = None, None, False, None
            self.state, self.error = "stopped", None
        log.info("%s：已停止", self.label)

    def restart(self) -> None:
        self.stop()
        self.start()

    def wait_ready(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state == "running":
                return True
            if self.state in ("stopped", "unresponsive"):
                return False
            time.sleep(0.5)
        return False


COMFY = Service(
    "comfyui", "ComfyUI 引擎",
    [str(config.COMFY_PYTHON), "-s", "ComfyUI\\main.py", "--listen", "127.0.0.1", "--port", str(config.COMFY_PORT),
     "--disable-auto-launch"],
    config.COMFY_DIR, config.COMFY_PORT, f"{config.COMFY_URL}/system_stats",
    config.LOGS_DIR / "comfyui.log", start_timeout=300)

def _painter_env() -> dict:
    from . import storage   # 延迟导入，避免循环依赖
    d = storage.get()["output_dir"]
    return {"PAINTER_OUTPUT_DIR": d} if d else {}


PAINTER = Service(
    "painter", "绘画模块",
    # 与前端用同一个 Python（根目录 .venv，依赖见 requirements.txt）
    [sys.executable, "-s", str(config.PAINTER_DIR / "run_painter.py")],
    config.PAINTER_DIR, config.PAINTER_PORT, f"{config.PAINTER_URL}/v1/health",
    config.LOGS_DIR / "painter.log", start_timeout=120, env_fn=_painter_env)

SERVICES = {s.name: s for s in (COMFY, PAINTER)}


def comfy_stats() -> dict | None:
    try:
        r = httpx.get(f"{config.COMFY_URL}/system_stats", timeout=2.0)
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    sysinfo = d.get("system", {})
    return {
        "version": sysinfo.get("comfyui_version"),
        "pytorch": sysinfo.get("pytorch_version"),
        "ram_total": sysinfo.get("ram_total"), "ram_free": sysinfo.get("ram_free"),
        "devices": [{"name": dv.get("name", "").split(" : ")[0], "vram_total": dv.get("vram_total"),
                     "vram_free": dv.get("vram_free")} for dv in d.get("devices", [])],
    }


def start_all() -> None:
    COMFY.start()
    PAINTER.start()


def stop_all() -> None:
    for s in (PAINTER, COMFY):
        try:
            s.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止 %s 失败", s.label)

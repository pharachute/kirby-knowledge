"""Personal Knowledge Base 1.0 — Windows 启动器。

双击桌面「Personal Knowledge Base」会执行本文件（经 ``launch_pkb.vbs`` 用 pythonw 隐藏启动）：

    1. 定位项目根目录（本文件在 ``<root>/launcher/`` 下）
    2. 若服务已在运行（/healthz 返回 ok）→ 直接打开浏览器，不再启动第二个实例
    3. 若端口被别的程序占用 → 明确报错（不静默失败）
    4. 启动 ``python -m personal_memory web``，等待 /healthz 真正 ready
    5. 打开默认浏览器 → http://127.0.0.1:8765
    6. 失败时写日志并弹出 Windows 原生提示框（不依赖任何第三方库）

手动控制：``python launcher/launch_pkb.py --stop`` 关闭服务；
``--foreground`` 在当前控制台前台运行（排障用，能看到服务输出）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

#: 项目根目录：本文件位于 <root>/launcher/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("PKB_HOST", "127.0.0.1")
PORT = int(os.environ.get("PKB_PORT", "8765"))
#: 与 CLI 默认一致：data/memory.db（可用 PERSONAL_MEMORY_DB 覆盖）
DB_PATH = Path(os.environ.get("PERSONAL_MEMORY_DB") or (PROJECT_ROOT / "data" / "memory.db"))
URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{URL}/healthz"
LOG_DIR = PROJECT_ROOT / "logs"
#: 本地模型配置：环境变量 > 用户目录（仓库内不放密钥，避免污染测试与提交）
USER_CONFIG_PATH = Path.home() / ".personal-memory" / "llm.json"
LAUNCH_LOG = LOG_DIR / "pkb-launch.log"
SERVER_LOG = LOG_DIR / "pkb-server.log"
PID_FILE = LOG_DIR / "pkb.pid"
READY_TIMEOUT_SECONDS = 40.0
POLL_INTERVAL_SECONDS = 0.4


def config_path() -> Path | None:
    """Locate the local LLM config: env override, then the per-user file."""
    override = os.environ.get("PERSONAL_MEMORY_LLM_CONFIG")
    if override and Path(override).expanduser().exists():
        return Path(override).expanduser()
    return USER_CONFIG_PATH if USER_CONFIG_PATH.exists() else None


def log(message: str) -> None:
    """Append one line to logs/pkb-launch.log (never raises)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LAUNCH_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def health_body(timeout: float = 1.5) -> str | None:
    """Return the body of /healthz when the PKB server answers, else None."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read().decode("utf-8", errors="replace").strip()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_pkb_running() -> bool:
    return health_body() == "ok"


def port_occupied() -> bool:
    """True when *something* listens on the port (may not be PKB)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.8)
        return probe.connect_ex((HOST, PORT)) == 0


def python_executable(*, console: bool = False) -> str:
    """The interpreter that runs PKB: prefer the one running this script."""
    candidate = Path(sys.executable)
    if console:
        return str(candidate)
    windowless = candidate.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else candidate)


def message_box(title: str, body: str) -> None:
    """Native Windows dialog so a hidden launch can still report failures."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, body, title, 0x10)  # MB_ICONERROR
    except Exception:  # pragma: no cover - non-Windows or blocked dialog
        pass


def fail(title: str, body: str) -> int:
    log(f"FAIL {title} :: {body}")
    message_box(title, body)
    return 1


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def stop_server() -> int:
    """Stop a launcher-started server (uses the pid file written at launch)."""
    pid = read_pid()
    if pid is None:
        print("没有找到由启动器记录的进程（logs/pkb.pid 不存在）。")
        return 0
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        return fail("Personal Knowledge Base 关闭失败", f"无法结束进程 {pid}：{error}")
    PID_FILE.unlink(missing_ok=True)
    log(f"stopped pid={pid}")
    print(f"已关闭 Personal Knowledge Base（pid {pid}）。")
    return 0


def start_server(*, foreground: bool) -> subprocess.Popen | int:
    """Start the web server; returns the process (background) or an exit code (foreground)."""
    command = [python_executable(console=foreground), "-m", "personal_memory", "web",
               "--db", str(DB_PATH), "--host", HOST, "--port", str(PORT)]
    local_config = config_path()
    if local_config is not None:
        command += ["--config", str(local_config)]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("start " + " ".join(command))
    if foreground:
        return subprocess.call(command, cwd=str(PROJECT_ROOT))
    server_log = SERVER_LOG.open("a", encoding="utf-8")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    return subprocess.Popen(command, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                            stdout=server_log, stderr=server_log, creationflags=creationflags)


def wait_until_ready(process: subprocess.Popen | None, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
    """Poll /healthz until the server answers ok, or the process dies / timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_body() == "ok":
            return True
        if process is not None and process.poll() is not None:
            return False       # the server exited early: no point waiting
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def tail(path: Path, lines: int = 12) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(没有日志)"
    return "\n".join(content[-lines:]) or "(没有日志)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Knowledge Base 启动器")
    parser.add_argument("--stop", action="store_true", help="关闭由启动器启动的服务")
    parser.add_argument("--foreground", action="store_true", help="在当前控制台前台运行（排障）")
    parser.add_argument("--no-browser", action="store_true", help="启动但不打开浏览器")
    args = parser.parse_args(argv)

    if args.stop:
        return stop_server()

    if is_pkb_running():
        log(f"already running -> open {URL}")
        if not args.no_browser:
            webbrowser.open(URL)
        print(f"Personal Knowledge Base 已经在运行：{URL}")
        return 0

    if port_occupied():
        return fail(
            "Personal Knowledge Base 启动失败",
            f"端口 {PORT} 已被其他程序占用。\n\n"
            f"请关闭占用该端口的程序后重新启动。\n\n"
            f"（也可以设置环境变量 PKB_PORT 换一个端口）\n\n"
            f"日志：{LAUNCH_LOG}",
        )

    started = start_server(foreground=args.foreground)
    if args.foreground:
        return int(started)

    process = started
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    if not wait_until_ready(process):
        return fail(
            "Personal Knowledge Base 启动失败",
            f"服务在 {int(READY_TIMEOUT_SECONDS)} 秒内没有就绪。\n\n"
            f"最近的服务输出（{SERVER_LOG}）：\n{tail(SERVER_LOG)}\n\n"
            f"启动日志：{LAUNCH_LOG}",
        )

    log(f"ready -> open {URL}")
    if not args.no_browser:
        webbrowser.open(URL)
    print(f"Personal Knowledge Base 已启动：{URL}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the desktop entry
    raise SystemExit(main())

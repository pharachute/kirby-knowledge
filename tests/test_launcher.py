"""冻结阶段：Windows 启动器测试（不启动真实服务，只验证启动逻辑与契约）。"""

from __future__ import annotations

import http.server
import importlib
import os
import pathlib
import socket
import subprocess
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER_DIR = ROOT / "launcher"
sys.path.insert(0, str(LAUNCHER_DIR))

import launch_pkb  # noqa: E402


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # keep the test output clean
        pass


class LauncherConfigTest(unittest.TestCase):
    def test_project_root_is_the_repository(self) -> None:
        self.assertTrue((launch_pkb.PROJECT_ROOT / "personal_memory").is_dir())
        self.assertTrue((launch_pkb.PROJECT_ROOT / "pyproject.toml").is_file())

    def test_defaults_match_the_application(self) -> None:
        from personal_memory.web.server import DEFAULT_HOST, DEFAULT_PORT

        self.assertEqual(launch_pkb.HOST, DEFAULT_HOST)
        self.assertEqual(launch_pkb.PORT, DEFAULT_PORT)
        self.assertEqual(launch_pkb.URL, f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
        self.assertEqual(launch_pkb.HEALTH_URL, launch_pkb.URL + "/healthz")

    def test_default_database_is_data_memory_db(self) -> None:
        self.assertEqual(launch_pkb.DB_PATH, launch_pkb.PROJECT_ROOT / "data" / "memory.db")

    def test_env_override_is_honoured(self) -> None:
        target = ROOT / "data" / "custom-freeze.db"
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, pathlib; sys.path.insert(0, str(pathlib.Path('launcher').resolve())); "
             "import launch_pkb as L; print(L.DB_PATH); print(L.PORT)"],
            cwd=str(ROOT), capture_output=True, text=True,
            env={**os.environ, "PERSONAL_MEMORY_DB": str(target), "PKB_PORT": "9911",
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[0], str(target))
        self.assertEqual(lines[1], "9911")

    def test_llm_config_lives_outside_the_repository(self) -> None:
        """仓库里不允许放本地模型配置：否则测试会读到真实密钥（联网、非 hermetic）。"""
        self.assertFalse((ROOT / "config" / "llm.json").exists())
        self.assertTrue((ROOT / "config" / "llm.example.json").is_file())
        self.assertEqual(launch_pkb.USER_CONFIG_PATH, pathlib.Path.home() / ".personal-memory" / "llm.json")

    def test_config_path_resolution(self) -> None:
        original = os.environ.get("PERSONAL_MEMORY_LLM_CONFIG")
        original_user = launch_pkb.USER_CONFIG_PATH
        try:
            os.environ.pop("PERSONAL_MEMORY_LLM_CONFIG", None)
            launch_pkb.USER_CONFIG_PATH = ROOT / "does-not-exist.json"
            self.assertIsNone(launch_pkb.config_path())          # 没有配置时不传 --config

            launch_pkb.USER_CONFIG_PATH = ROOT / "tests" / "test_launcher.py"
            self.assertEqual(launch_pkb.config_path(), launch_pkb.USER_CONFIG_PATH)

            os.environ["PERSONAL_MEMORY_LLM_CONFIG"] = str(ROOT / "pyproject.toml")
            self.assertEqual(launch_pkb.config_path(), ROOT / "pyproject.toml")   # 环境变量优先
        finally:
            launch_pkb.USER_CONFIG_PATH = original_user
            if original is None:
                os.environ.pop("PERSONAL_MEMORY_LLM_CONFIG", None)
            else:
                os.environ["PERSONAL_MEMORY_LLM_CONFIG"] = original

    def test_python_executable_prefers_pythonw_without_a_console(self) -> None:
        windowless = launch_pkb.python_executable()
        console = launch_pkb.python_executable(console=True)
        self.assertTrue(windowless)
        self.assertTrue(console)
        if os.name == "nt":
            self.assertTrue(windowless.endswith("pythonw.exe") or not windowless.endswith("python.exe"))

    def test_importing_the_launcher_does_not_touch_anything(self) -> None:
        """导入模块只是定义函数：不启动服务、不写日志、不建数据库。"""
        module = importlib.reload(launch_pkb)
        self.assertEqual(module.PROJECT_ROOT, ROOT)
        self.assertIsInstance(module.DB_PATH, pathlib.Path)
        self.assertTrue(callable(module.main))
        self.assertFalse((ROOT / "data" / "memory.db.tmp").exists())


class HealthProbeTest(unittest.TestCase):
    """health / 端口占用判定：用本地 stub server 验证，不启动真服务。"""

    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

        self.unused_port = self._free_port()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_health_answers_ok_against_a_pkb_like_server(self) -> None:
        original = launch_pkb.HEALTH_URL
        launch_pkb.HEALTH_URL = f"http://127.0.0.1:{self.port}/healthz"
        try:
            self.assertEqual(launch_pkb.health_body(), "ok")
            self.assertTrue(launch_pkb.is_pkb_running())
        finally:
            launch_pkb.HEALTH_URL = original

    def test_health_is_none_when_nothing_listens(self) -> None:
        original = launch_pkb.HEALTH_URL
        launch_pkb.HEALTH_URL = f"http://127.0.0.1:{self.unused_port}/healthz"
        try:
            self.assertIsNone(launch_pkb.health_body())
            self.assertFalse(launch_pkb.is_pkb_running())
        finally:
            launch_pkb.HEALTH_URL = original

    def test_port_occupied_detection(self) -> None:
        original_host, original_port = launch_pkb.HOST, launch_pkb.PORT
        launch_pkb.HOST, launch_pkb.PORT = "127.0.0.1", self.port
        try:
            self.assertTrue(launch_pkb.port_occupied())
        finally:
            launch_pkb.HOST, launch_pkb.PORT = original_host, original_port
        launch_pkb.HOST, launch_pkb.PORT = "127.0.0.1", self.unused_port
        try:
            self.assertFalse(launch_pkb.port_occupied())
        finally:
            launch_pkb.HOST, launch_pkb.PORT = original_host, original_port

    def test_already_running_launch_opens_without_starting_a_server(self) -> None:
        """服务已在运行时：启动器应直接返回 0，不启动第二个实例。"""
        original_url, original_host, original_port = launch_pkb.HEALTH_URL, launch_pkb.HOST, launch_pkb.PORT
        launch_pkb.HEALTH_URL = f"http://127.0.0.1:{self.port}/healthz"
        launch_pkb.HOST, launch_pkb.PORT = "127.0.0.1", self.port
        try:
            code = launch_pkb.main(["--no-browser"])
        finally:
            launch_pkb.HEALTH_URL, launch_pkb.HOST, launch_pkb.PORT = original_url, original_host, original_port
        self.assertEqual(code, 0)

    def test_occupied_port_by_another_program_is_a_clear_failure(self) -> None:
        """端口被非 PKB 程序占用：必须报错（端口号写进提示），而不是静默失败。

        用「另一个会接受连接、但 /healthz 不返回 ok 的 HTTP 服务」模拟别的程序占端口。
        注意不能用 listen(1) 且不 accept 的裸 socket：backlog 满之后第二次探测会被拒绝，
        那样就测不出真实行为。
        """
        foreign = http.server.ThreadingHTTPServer(("127.0.0.1", self.unused_port), _HealthHandler)
        foreign.handle_error = lambda *args: None
        # 让它对 /healthz 返回 404（不是 PKB，也不回答路径）
        class _Foreign(_HealthHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

        foreign.RequestHandlerClass = _Foreign
        thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        original_url, original_host, original_port = launch_pkb.HEALTH_URL, launch_pkb.HOST, launch_pkb.PORT
        launch_pkb.HEALTH_URL = f"http://127.0.0.1:{self.unused_port}/healthz"
        launch_pkb.HOST, launch_pkb.PORT = "127.0.0.1", self.unused_port
        messages: list[tuple[str, str]] = []
        original_box = launch_pkb.message_box
        launch_pkb.message_box = lambda title, body: messages.append((title, body))
        try:
            self.assertTrue(launch_pkb.port_occupied())          # 端口确实被占
            self.assertFalse(launch_pkb.is_pkb_running())        # 但不是 PKB
            code = launch_pkb.main(["--no-browser"])
        finally:
            launch_pkb.message_box = original_box
            launch_pkb.HEALTH_URL, launch_pkb.HOST, launch_pkb.PORT = original_url, original_host, original_port
            foreign.shutdown()
        self.assertEqual(code, 1)
        self.assertTrue(messages, "启动失败必须弹出明确提示")
        self.assertIn(str(self.unused_port), messages[0][1])


class LauncherFileTest(unittest.TestCase):
    """入口文件的静态契约：路径、编码、可双击。"""

    def test_launcher_files_exist(self) -> None:
        for name in ("launch_pkb.py", "launch_pkb.vbs", "stop_pkb.py", "install_shortcut.ps1"):
            self.assertTrue((LAUNCHER_DIR / name).is_file(), name)

    def test_vbs_is_ascii_only(self) -> None:
        """VBScript 按 ANSI 读取：非 ASCII 会乱码甚至破坏语法。"""
        raw = (LAUNCHER_DIR / "launch_pkb.vbs").read_bytes()
        raw.decode("ascii")   # 不抛异常即纯 ASCII
        self.assertIn(b"launch_pkb.py", raw)

    def test_shortcut_installer_is_ascii_only(self) -> None:
        (LAUNCHER_DIR / "install_shortcut.ps1").read_bytes().decode("ascii")

    def test_stop_script_reuses_the_launcher(self) -> None:
        text = (LAUNCHER_DIR / "stop_pkb.py").read_text(encoding="utf-8")
        self.assertIn("from launch_pkb import stop_server", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

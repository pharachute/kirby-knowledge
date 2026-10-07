"""关闭由启动器启动的 Personal Knowledge Base（读取 logs/pkb.pid）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from launch_pkb import stop_server  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(stop_server())

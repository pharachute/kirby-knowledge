# Windows 启动说明（Personal Knowledge Base 1.0）

## 1. 怎么启动

桌面上双击 **Personal Knowledge Base** 即可。

- 桌面入口：`C:\Users\<你>\Desktop\Personal Knowledge Base.lnk`
- 它实际执行：`D:\python\pythonw.exe "D:\DSH-worlp\personal-memory-system\launcher\launch_pkb.py"`
- 启动器会：启动本地服务 → 轮询 `/healthz` 直到真正就绪 → 自动打开默认浏览器 → `http://127.0.0.1:8765`
- 不会出现需要输入命令的窗口；服务以隐藏方式在后台运行。

如果项目文件夹换了位置，重新运行一次 `launcher\install_shortcut.ps1` 即可重建桌面入口。

## 2. PKB 实际在哪里运行

| 项目 | 位置 |
| --- | --- |
| 项目根目录 | `D:\DSH-worlp\personal-memory-system` |
| 数据服务 | 本机 `127.0.0.1:8765`（只监听本机，不对外开放） |
| 数据库 | `D:\DSH-worlp\personal-memory-system\data\memory.db` |
| 上传/导入临时文件 | `data\.pkb-uploads\`（每次导入后自动清理） |
| 启动器日志 | `logs\pkb-launch.log` |
| 服务输出日志 | `logs\pkb-server.log` |
| 进程号（启动器写入） | `logs\pkb.pid` |
| 模型配置（含 API Key） | `C:\Users\<你>\.personal-memory\llm.json` —— **仓库内不放密钥**（模板见 `config\llm.example.json`）；启动器会以 `--config` 显式传给服务 |

## 3. 如何关闭

- **推荐**：运行 `python launcher\stop_pkb.py`（读取 `logs\pkb.pid` 并结束该进程）。
- 也可以：任务管理器里结束 `python.exe`（即 Personal Knowledge Base 服务进程）。
- 关闭后数据已经写入 SQLite，重新启动即可继续使用。

## 3.5 模型配置放在哪里（Capture 需要）

Capture / 导入需要 LLM 密钥。密钥只放在**用户目录**，仓库里保持干净（否则测试会读到真实密钥、并且可能把密钥提交上去）：

```text
C:\Users\<你>\.personal-memory\llm.json      # 例：{"provider":"deepseek","model":"deepseek-flash","base_url":"https://api.deepseek.com","api_key":"sk-..."}
```

- 启动器按 `PERSONAL_MEMORY_LLM_CONFIG` → `~\.personal-memory\llm.json` 的顺序查找，找到就传给服务；
- 没有配置文件时，浏览、搜索、Memories、Sources 全部可用，只有 Capture / 导入会给出明确的中文提示；
- 也可以直接用环境变量 `PERSONAL_MEMORY_LLM_API_KEY` 临时提供密钥。

## 4. 启动失败去哪里看

1. `logs\pkb-launch.log` —— 启动器自己的记录（启动命令、超时、失败原因）。
2. `logs\pkb-server.log` —— 服务进程的输出（Python 报错在这里）。
3. 启动失败时会弹出 Windows 原生提示框，内容包含原因与日志路径，常见两类：
   - `端口 8765 已被其他程序占用`：关闭占用端口的程序，或设置环境变量 `PKB_PORT` 换端口。
   - `服务在 40 秒内没有就绪`：看 `logs\pkb-server.log` 里的报错（通常是模型配置或数据库文件权限）。

## 5. 开发者：如何重新得到一个空数据库

```powershell
# 1) 先停掉服务（数据库文件被占用时无法删除）
D:\python\python.exe launcher\stop_pkb.py

# 2) 删除当前运行数据库（用户数据）
Remove-Item D:\DSH-worlp\personal-memory-system\data\memory.db

# 3) 重新启动（服务会自动建库建表）
D:\python\python.exe launcher\launch_pkb.py --no-browser
#    或直接双击桌面入口
```

验证空库：

```powershell
D:\python\python.exe -c "from personal_memory import Database, MemoryRepository; import pathlib; r=MemoryRepository(Database(pathlib.Path(r'data/memory.db'))); print(r.counts())"
# 期望：{'sources': 0, 'memories': 0, 'memory_sources': 0}
```

前台排障（能看到服务输出，不隐藏窗口）：

```powershell
D:\python\python.exe launcher\launch_pkb.py --foreground --no-browser
```

不使用启动器时，也可以直接用 CLI（与启动器等价）：

```powershell
D:\python\python.exe -m personal_memory web --db data\memory.db --host 127.0.0.1 --port 8765
```

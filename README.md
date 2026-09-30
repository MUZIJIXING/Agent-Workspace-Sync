# Agent Workspace Sync

**让不同 AI 编程工具在同一个本地项目中可靠地轮流工作。**

前一个工具说“交接一下”，保存目标、已完成内容、真实待办和验证结果并释放占用；
下一个工具说“继续”，读取交接后接着做。

```text
Claude Code 开发 → 交接并释放 → DSH 接手 → 交接并释放 → ZCode 继续
```

## 为什么用它

- **保留工作上下文。** 将目标、进度、待办、决策和验证结果保存在项目中，换工具后直接接着做。
- **协调开发占用。** 一个工作区由一个会话持有，guard 在修改前核对当前会话的占用权限。
- **项目独立运行。** 每个项目拥有自己的身份、SQLite 数据库和客户端接入文件。
- **适配现有工具。** 支持 Git 和非 Git 项目，沿用各客户端的模型接入；核心基于 Python 标准库和 SQLite。

## 实测：三个工具做一个网页

Claude Code、DSH 和 ZCode 依次制作 **Focus Garden** 中文任务板。
后两个工具通过“继续”读取前一工具的交接，完成下一阶段。

| 阶段 | 工具与实测入口 | 实际完成 |
| --- | --- | --- |
| 1 | Claude Code 2.1.237，原生 CLI | HTML/CSS、语义结构、桌面与移动端骨架 |
| 2 | DSH 0.1.1-rc.2，原生 Web 客户端 | 添加、完成/取消、删除、筛选、统计、空状态、存储、主题 |
| 3 | ZCode CLI 0.16.5，app-server | 可访问性、焦点、主题状态、存储回退 |

### 实际接手与交接

**Claude Code：guard 检查开发占用。**

![Claude Code 在没有 active 所有者时被 guard 拦截](docs/assets/harness-claude-guard-annotated.png)

**Claude Code：保留下一阶段待办，等待交接。**

![Claude Code 保留工作区占用，并明确记录下一阶段的真实待办](docs/assets/harness-claude-pending-annotated.png)

**Claude Code：保存上下文与待办，释放占用。**

![Claude Code 原生会话中交接完成，租约状态为 released](docs/assets/harness-claude-handoff-annotated.png)

**Claude → DSH：只说“继续”，检查工作区、接手占用，再接着做页面交互。**

![DSH 收到继续后检查工作区、接手占用并延续真实待办](docs/assets/harness-dsh-resume-annotated.png)

**DSH → ZCode：通过“交接一下”保存交接并释放占用。**

![DSH 调用 workspace_leave 完成交接并释放工作区](docs/assets/harness-dsh-handoff-annotated.png)

**ZCode：收到“继续”，加载接入规则，通过 MCP 检查工作区。**

![ZCode 原生 App 中加载 agent-workspace-sync 并检查工作区状态](docs/assets/harness-zcode-resume-annotated.png)

**ZCode：确认占用，保存交接并释放。**

![ZCode 原生 App 中调用 workspace_leave 并报告交接完成](docs/assets/harness-zcode-handoff-annotated.png)

### 网页成果

**Claude：页面骨架。**

![Claude 完成页面骨架](docs/assets/web-relay-01-claude.png)

**DSH：接上交互。** 第四条任务通过浏览器 Enter 添加。

![DSH 完成网页交互](docs/assets/web-relay-02-dsh.png)

**ZCode：补齐边界后的最终桌面页面。**

![三工具接力后的最终网页](docs/assets/web-relay-03-zcode-desktop.png)

**390px 移动端实测。**

<img src="docs/assets/web-relay-03-zcode-mobile.png" alt="最终网页移动端布局" width="390">

## 开始使用

### 1. 安装

需要 Python 3.10+ 和已配置模型的客户端。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

### 2. 配置项目

以 Claude Code 为例，将 `examples/focus-garden` 换成你的项目路径：

```powershell
.\.venv\Scripts\aws.exe init examples/focus-garden
.\.venv\Scripts\aws.exe setup examples/focus-garden --client claude-code
.\.venv\Scripts\aws.exe doctor examples/focus-garden
```

每个项目执行一次 `init`，为要使用的每个客户端执行一次 `setup`。
客户端参数可选 `claude-code`、`dsh`、`zcode`。
将生成的 `.agent-workspace/` 加入项目的 `.gitignore`。

macOS/Linux 将安装命令中的 Python 路径换成 `.venv/bin/python`，操作命令使用 `.venv/bin/aws`。

### 3. 在客户端加载

加载项目内 `.agent-workspace/clients/<client>/` 中的接入文件：

| 客户端 | 加载内容 |
| --- | --- |
| Claude Code | 用 `--plugin-dir` 加载插件目录，用 `--mcp-config` 和 `--strict-mcp-config` 加载 `mcp.json` |
| DSH | 通过 `--patch` 加载 `bundles/agent-workspace-sync/cordis.patch.yml` |
| ZCode | 加载 `marketplace.json` 中的 Agent Workspace Sync 插件 |

确认 MCP、交接指令和 guard 已启用，即可开始交接。

<details>
<summary>Claude Code / DSH 启动示例</summary>

在目标项目目录执行对应命令，沿用客户端已有的模型配置。

```powershell
# Claude Code
claude --plugin-dir .agent-workspace/clients/claude-code/plugins/agent-workspace-sync --mcp-config .agent-workspace/clients/claude-code/mcp.json --strict-mcp-config

# DSH
dsh --patch .agent-workspace/clients/dsh/bundles/agent-workspace-sync/cordis.patch.yml
```

</details>

### 4. 日常交接

1. 在项目中说“继续”，客户端检查状态、取得占用并读取待办。
2. 完成本轮工作后说“交接一下”，保存交接记录并释放占用。
3. 交接成功后，在下一工具中打开同一项目，说“继续”。

每个工作区同时由一个会话开发。阶段完成后占用持续到交接或释放；占用过期时，
确认旧工具已停止修改项目，再向新工具明确提出“接管这个项目”。

可用以下命令查看占用状态和最新交接：

```powershell
.\.venv\Scripts\aws.exe status examples/focus-garden
.\.venv\Scripts\aws.exe handoff latest examples/focus-garden
```

当前实机覆盖 Windows 的 Claude Code、DSH 和 ZCode CLI 接力。

采用 [MIT License](LICENSE)。

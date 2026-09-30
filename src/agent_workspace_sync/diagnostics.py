"""Read-only onboarding checks; never acquire, initialize, or repair a workspace."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .handoff import HandoffError
from .ownership import OwnershipError
from .session import SessionError
from .storage import StorageError
from .workflow import FRESHNESS_STALE, WorkflowError, inspect_workspace
from .workspace import WorkspaceError, WorkspaceNotInitializedError

EXPECTED_TOOLS = frozenset({
    "workspace_inspect", "workspace_enter", "workspace_heartbeat",
    "workspace_leave", "workspace_takeover",
})


def probe_runtime() -> dict:
    """Probe this interpreter using a real stdio server, without tool mutations."""
    try:
        from importlib.metadata import version
        from mcp import Client, StdioServerParameters
        from .mcp_server import MCPServer
    except ImportError:
        return {"status": "error", "code": "mcp_unavailable"}
    if MCPServer is None:
        return {"status": "error", "code": "mcp_unavailable"}

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_workspace_sync.mcp_server"],
            cwd=str(Path.cwd()),
            env=dict(os.environ),
        )
        async with Client(parameters) as client:
            listing = await client.list_tools()
            return {tool.name for tool in listing.tools}

    try:
        names = asyncio.run(asyncio.wait_for(check(), timeout=15))
        if names != EXPECTED_TOOLS:
            return {"status": "error", "code": "mcp_tools_mismatch"}
        return {"status": "ok", "code": "mcp_ready", "python": sys.executable,
                "mcp_version": version("mcp")}
    except Exception:
        # Child stderr remains available to a direct diagnostic invocation.
        # Do not copy arbitrary child logs or environment values into reports.
        return {"status": "error", "code": "mcp_start_failed"}


def check_runtime(python: str) -> dict:
    """Execute the probe with the user's selected interpreter, never a shell."""
    try:
        completed = subprocess.run(
            [python, "-m", "agent_workspace_sync.diagnostics"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25,
        )
    except OSError:
        return {"status": "error", "code": "python_unavailable"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "code": "mcp_timeout"}
    if completed.returncode != 0:
        return {"status": "error", "code": "package_or_runtime_unavailable"}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "code": "invalid_probe_result"}
    valid_results = {
        ("ok", "mcp_ready"),
        ("error", "mcp_unavailable"),
        ("error", "mcp_tools_mismatch"),
        ("error", "mcp_start_failed"),
    }
    if (not isinstance(result, dict)
            or not isinstance(result.get("status"), str)
            or not isinstance(result.get("code"), str)
            or (result["status"], result["code"]) not in valid_results):
        return {"status": "error", "code": "invalid_probe_result"}
    return result


def diagnose(path: Path | str, python: str) -> dict:
    """Inspect prerequisites and workspace state, with explicit verification limits."""
    runtime = check_runtime(python)
    try:
        view = inspect_workspace(path)
    except WorkspaceNotInitializedError:
        workspace = {"status": "error", "code": "workspace_not_initialized"}
    except (WorkspaceError, StorageError, SessionError, OwnershipError, HandoffError, WorkflowError):
        workspace = {"status": "error", "code": "workspace_invalid"}
    else:
        if view.lease is None:
            code = "workspace_available"
        elif view.lease_freshness == FRESHNESS_STALE:
            code = "workspace_stale"
        else:
            code = "workspace_owned"
        workspace = {
            "status": "ok", "code": code, "root": str(view.workspace_root),
            "owner": view.owner_session.harness_type if view.owner_session else None,
            "has_handoff": view.latest_handoff is not None,
        }
    return {
        "runtime": runtime,
        "workspace": workspace,
        "guard": {"status": "unverified", "code": "client_guard_unverified"},
        "ready_for_client_check": runtime["status"] == workspace["status"] == "ok",
        "protection_verified": False,
    }


_MESSAGES = {
    "python_unavailable": "无法启动指定的 Python。请检查解释器路径。",
    "package_or_runtime_unavailable": "指定 Python 无法运行诊断。请在该环境安装本项目及 [mcp] 可选依赖。",
    "mcp_unavailable": "缺少兼容的 MCP SDK。请在指定 Python 环境安装本项目的 [mcp] 可选依赖。",
    "mcp_ready": "MCP 服务已通过真实 stdio 连接检查，五个工作区工具均可用。",
    "mcp_timeout": "MCP 启动检查超时。请检查该 Python 环境及本地进程启动限制。",
    "mcp_start_failed": "MCP 服务未能正常启动。请检查该 Python 环境的依赖及启动诊断。",
    "mcp_tools_mismatch": "MCP 工具列表不符合当前版本。请核对安装的项目版本。",
    "invalid_probe_result": "诊断进程返回了无法识别的结果。请核对安装的项目版本。",
    "workspace_not_initialized": "项目尚未接入。确认项目路径后，显式运行 init 初始化。",
    "workspace_invalid": "工作区状态损坏、不可读或不一致。请检查项目内状态，勿删除状态后重新初始化。",
    "workspace_available": "项目当前没有开发占用。工具接入验证后，可在工具中说“继续”。",
    "workspace_owned": "项目已有工具在使用。仅原持有实例可继续；其他实例须等待原工具交接。",
    "workspace_stale": "上次占用已过期，尚不能确认原工具停止。确认其已停止修改后，再明确要求恢复或接管。",
    "client_guard_unverified": "尚未验证客户端已加载 guard。请在隔离演示项目中完成占用拦截检查；服务连通不代表保护已生效。",
}


def describe(report: dict) -> str:
    lines = ["Agent Workspace Sync 接入检查"]
    for name, label in (("runtime", "运行环境"), ("workspace", "项目状态"), ("guard", "客户端保护")):
        check = report[name]
        lines.append(f"{label}：{_MESSAGES.get(check['code'], '检查失败，请核对安装环境。')}")
    if report["workspace"].get("owner"):
        lines.append(f"占用工具：{report['workspace']['owner']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps(probe_runtime(), ensure_ascii=True))

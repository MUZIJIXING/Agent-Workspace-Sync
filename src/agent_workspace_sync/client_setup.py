"""Prepare reviewable, project-local client bundles from versioned templates.

Preparation does not register a plugin, launch a client, or grant ownership.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .diagnostics import check_runtime
from .workflow import inspect_workspace


class ClientSetupError(Exception):
    """Setup could not safely prepare the requested local bundle."""


_FILES = {
    "zcode": (
        "marketplace.json",
        "plugins/agent-workspace-sync/.zcode-plugin/plugin.json",
        "plugins/agent-workspace-sync/.mcp.json",
        "plugins/agent-workspace-sync/hooks/hooks.json",
        "plugins/agent-workspace-sync/skills/agent-workspace-sync/SKILL.md",
    ),
    "dsh": (
        "bundles/agent-workspace-sync/package.json",
        "bundles/agent-workspace-sync/cordis.patch.yml",
        "bundles/agent-workspace-sync/lib/index.js",
        "bundles/agent-workspace-sync/protocol.md",
    ),
    "claude-code": (
        "plugins/agent-workspace-sync/.claude-plugin/plugin.json",
        "plugins/agent-workspace-sync/hooks/hooks.json",
        "plugins/agent-workspace-sync/skills/agent-workspace-sync/SKILL.md",
    ),
}


def _json(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _read_templates(source: Path, client: str) -> dict[str, str]:
    contents = {}
    for name in _FILES[client]:
        path = source / client / name
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ClientSetupError("接入模板不能通过链接读取源目录之外的文件。")
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ClientSetupError(
                "接入模板缺失或无法读取。请重新安装当前版本，或用 --source 指定完整的 integrations 目录。"
            ) from error
    return contents


def _bind_python(contents: dict[str, str], client: str, python: str, target: Path) -> None:
    if client == "zcode":
        prefix = "plugins/agent-workspace-sync/"
        try:
            mcp = json.loads(contents[prefix + ".mcp.json"])
            hooks = json.loads(contents[prefix + "hooks/hooks.json"])
            mcp["mcpServers"]["agent-workspace-sync"]["command"] = python
            for entries in hooks["hooks"].values():
                for entry in entries:
                    for hook in entry["hooks"]:
                        hook["command"] = python
        except (ValueError, KeyError, TypeError) as error:
            raise ClientSetupError("ZCode 接入模板格式不兼容，请核对仓库版本。") from error
        contents[prefix + ".mcp.json"] = _json(mcp)
        contents[prefix + "hooks/hooks.json"] = _json(hooks)
    elif client == "claude-code":
        prefix = "plugins/agent-workspace-sync/"
        try:
            hooks = json.loads(contents[prefix + "hooks/hooks.json"])
            for entries in hooks["hooks"].values():
                for entry in entries:
                    for hook in entry["hooks"]:
                        parts = hook["command"].split()
                        if parts not in (
                            ["python", "-m", "agent_workspace_sync.claude_hook", "session-start"],
                            ["python", "-m", "agent_workspace_sync.claude_hook", "guard-pre-tool"],
                        ):
                            raise ValueError("unrecognized Claude hook command")
                        # Exec form preserves spaces and shell metacharacters
                        # in interpreter paths without any shell expansion.
                        hook["command"] = python
                        hook["args"] = parts[1:]
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ClientSetupError("Claude Code 接入模板格式不兼容，请核对仓库版本。") from error
        contents[prefix + "hooks/hooks.json"] = _json(hooks)
        contents["mcp.json"] = _json({"mcpServers": {"agent-workspace-sync": {
            "type": "stdio", "command": python,
            "args": ["-m", "agent_workspace_sync.mcp_server"],
            "env": {"PYTHONUTF8": "1"},
        }}})
    else:
        # JSON strings are YAML scalars too: no shell interpolation, !!js or
        # environment lookup is involved in either interpreter selection.
        quoted = json.dumps(python, ensure_ascii=True)
        # DSH's module loader accepts file URLs. Loading the local adapter
        # directly avoids installing a second package into the user's profile.
        adapter = json.dumps((target / "bundles/agent-workspace-sync/lib/index.js").as_uri())
        contents["bundles/agent-workspace-sync/cordis.patch.yml"] = (
            "- insert:\n"
            "    - id: agent-workspace-sync-dsh-adapter\n"
            f"      name: {adapter}\n"
            "      config:\n"
            f"        python: {quoted}\n"
            "    - id: agent-workspace-sync-mcp\n"
            "      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n"
            "        serverName: agent-workspace-sync\n"
            "        transport: stdio\n"
            f"        command: {quoted}\n"
            "        args: ['-m', 'agent_workspace_sync.mcp_server']\n"
            "        env:\n"
            "          PYTHONUTF8: '1'\n"
            "        failOnStartupError: true\n"
        )


def _prepare_dsh_preset(contents: dict[str, str], source: Path, root: Path) -> None:
    """Derive a project-only preset from the explicitly selected installed one.

    Preserve the client's tools and !!js expressions without adding a YAML
    dependency. Only the two verified standard row shapes are supported; a
    changed client composition must be reviewed rather than guessed at.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ClientSetupError("无法读取 DSH standard preset。请用 --dsh-preset 指定已安装客户端的 agent.cordis.yml。") from error
    instruction = (
        "- id: agent-instructions\n"
        "  name: '@deepseek-ai/dsh-agent-instructions'\n"
        "  config:\n"
        "    maxBytes: 65536\n"
    )
    skills = "- id: skill-filesystem\n  name: '@deepseek-ai/dsh-skill-filesystem'\n"
    for identity, block in (("agent-instructions", instruction), ("skill-filesystem", skills)):
        row = re.search(rf"(?ms)^- id: {identity}\n.*?(?=^- id: |\Z)", text)
        if (len(re.findall(rf"(?m)^\s*- id: {identity}\s*$", text)) != 1
                or text.count(block) != 1
                or row is None or not row.group().startswith(block)
                or any(line.strip() and not line.lstrip().startswith("#")
                       for line in row.group()[len(block):].splitlines())):
            raise ClientSetupError(
                "DSH preset 格式与已验证的 standard 不兼容，未生成配置。"
                "请核对客户端版本；当前已验证 0.1.1-rc.2，不会猜测或覆盖已有发现设置。"
            )
    text = text.replace(instruction, instruction + "    projectRootMarkers: ['.agent-workspace']\n")
    directories = [root / ".dsh/skills", root / ".agents/skills"]
    for directory in directories:
        if (directory.is_symlink() or directory.parent.is_symlink()
                or directory.resolve() != directory):
            raise ClientSetupError("DSH 技能目录存在链接，无法确认项目内发现范围，未生成配置。")
    text = text.replace(skills, skills + (
        "  config:\n"
        "    includeDefaultRoots: false\n"
        "    watch: false\n"
        f"    customSkillDirs: {json.dumps([str(p) for p in directories], ensure_ascii=True)}\n"
    ))
    prefix = "home/.agent-presets/awsync-workspace/"
    contents[prefix + "agent.cordis.yml"] = text
    contents[prefix + "preset.yml"] = (
        "name: Agent Workspace Sync\n"
        "description: Project-local instruction and skill discovery. Use a new session.\n"
    )
    contents["bundles/agent-workspace-sync/cordis.patch.yml"] += (
        "- id: agent-presets\n"
        "  config:\n"
        "    default: awsync-workspace\n"
    )


def prepare_client(
    path: Path | str, client: str, python: str, source: Path | str | None = None,
    *, dsh_preset: Path | str | None = None,
) -> Path:
    """Generate a complete bundle or reuse an identical one; never overwrite."""
    if client not in _FILES:
        raise ClientSetupError("暂只支持 zcode、dsh 和 claude-code 的项目内接入生成。")
    if dsh_preset is not None and client != "dsh":
        raise ClientSetupError("--dsh-preset 仅用于 DSH 接入，未生成配置。")
    # This verifies identity, database and domain state, without implicit init.
    view = inspect_workspace(path)
    if view.lease is not None:
        raise ClientSetupError("项目仍有开发占用。请先在原工具完成交接，再准备接入配置。")
    root = view.workspace_root.resolve()
    state = root / ".agent-workspace"
    parent = state / "clients"
    target = parent / client
    for location in (state, parent, target):
        if location.is_symlink() or location.resolve() != location:
            raise ClientSetupError("接入目录存在链接，无法确认项目内隔离，已停止。")

    if source is None:
        bundled = Path(__file__).resolve().parent / "_templates"
        source = bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / "integrations"
    else:
        source = Path(source)
    contents = _read_templates(source, client)
    runtime = check_runtime(python)
    if runtime.get("status") != "ok":
        raise ClientSetupError("指定 Python 未通过 MCP 检查。请先运行 doctor --python 检查该环境。")
    selected = runtime.get("python")
    if not isinstance(selected, str) or not Path(selected).is_absolute() or not Path(selected).is_file():
        raise ClientSetupError("诊断未返回有效的 Python 绝对路径，未生成配置。")
    _bind_python(contents, client, selected, target)
    if dsh_preset is not None:
        _prepare_dsh_preset(contents, Path(dsh_preset), root)
    contents["setup.json"] = _json({
        "client": client, "python": selected, "workspace_id": view.workspace_id,
        "mcp_version": runtime.get("mcp_version"), "client_guard_verified": False,
    })

    if target.exists():
        for name, expected in contents.items():
            existing = target / name
            if existing.is_symlink() or existing.resolve() != existing:
                raise ClientSetupError("已有接入文件存在链接，未覆盖任何配置。")
            try:
                matches = existing.read_text(encoding="utf-8") == expected
            except (OSError, UnicodeError):
                matches = False
            if not matches:
                raise ClientSetupError(
                    "已有接入配置与本次生成内容不同，未覆盖。请保留并检查旧目录，"
                    "需要更新时由你先将它移到项目内备份位置，再运行 setup。"
                )
        return target

    try:
        parent.mkdir(exist_ok=True)
        # Keep staging names short: a full UUID plus client name can exceed
        # Windows path limits even when the published destination is valid.
        staging = Path(tempfile.mkdtemp(prefix=f".{client}-", dir=parent))
    except OSError as error:
        raise ClientSetupError(
            "无法创建接入目录，未生成配置。请检查项目目录权限和磁盘空间后重试。"
        ) from error
    try:
        try:
            for name, content in contents.items():
                output = staging / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(content, encoding="utf-8", newline="\n")
        except OSError as error:
            raise ClientSetupError(
                "接入文件写入失败，未发布配置。请检查项目目录权限和磁盘空间后重试。"
            ) from error
        try:
            os.rename(staging, target)
        except OSError as error:
            raise ClientSetupError("接入目录发布失败或已被其他操作创建；请检查后重试。") from error
    finally:
        # Delete only this invocation's staging directory, within this project.
        try:
            if staging.exists() and staging.resolve().parent == parent.resolve() and staging.resolve().is_relative_to(root):
                shutil.rmtree(staging)
        except OSError as error:
            raise ClientSetupError(
                "接入配置未发布，临时目录清理失败。请检查权限并清理以下项目内"
                f"临时目录后重试：{staging}"
            ) from error
    return target

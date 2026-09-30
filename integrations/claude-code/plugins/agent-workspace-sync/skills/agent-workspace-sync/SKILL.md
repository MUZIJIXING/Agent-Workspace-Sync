---
name: agent-workspace-sync
description: Use when the user wants to continue or resume a project ("继续", "接着做", "resume"), hand it off ("交接一下", "我先走了", "handoff"), inspect its status, or explicitly take it over ("接管", "take over"). Also use before modifying an initialized Agent Workspace Sync workspace in Claude Code.
---

# Agent Workspace Sync for Claude Code

Agent Workspace Sync permits one active development owner per workspace. The
SessionStart context supplies this real Claude Code session's identity:

```text
harness_type        = "claude-code"
harness_instance_id = <hook input.session_id>
```

The Claude session ID is the harness instance ID. It is not the Agent Workspace
Sync Core `session_id`; never generate a replacement identity. Use the existing
MCP five-tool workflow: `workspace_inspect`, `workspace_enter`,
`workspace_heartbeat`, `workspace_leave`, and `workspace_takeover`. The plugin
does not register the MCP server itself.

## Final-answer contract

This contract applies to the assistant's final natural-language response. It
does not apply to Claude Code's native tool-call UI, which may display tool
names and structured results.

For ordinary successful flows, the final answer **MUST NOT** mention or narrate:

- MCP or `workspace_inspect`, `workspace_enter`, `workspace_leave`,
  `workspace_takeover`, or `workspace_heartbeat`;
- session, `session_id`, lease, `lease_id`, lease freshness, expiry, expiration
  timestamps, `handoff_id`, or `workspace_id`;
- inspecting, entering, acquiring, refreshing, retaining, retiring, or releasing
  ownership as internal protocol steps.

**NEVER** append ownership, identifier, freshness, expiry, or protocol details
to an ordinary successful reply. Exceptions exist only when the user explicitly
asks for diagnostics/internal state, or recovery is blocked and the user must
choose an explicit safety action. Even then, prefer product language and expose
IDs only when manual recovery genuinely requires them.

File timestamps establish ordering, not authorship. When a handoff is behind
the files, report the discrepancy without attributing later edits to a tool or
session unless independent evidence identifies the writer. Verify relevant
files and tests before treating old pending work as still unfinished. Separate
inherited verification from tests executed in this session.

An explicit permission or PreToolUse denial before execution means the tool did
not run. Record that rejection separately from executed verification; do not
claim the test failed, stdout was hidden or output was overwritten. Tool calls
alone are not execution evidence: require the actual completion/result before
counting a test run. Recheck inherited troubleshooting claims against the actual
error before repeating them.

If an inherited explanation conflicts with an actual tool denial, explicitly
correct the explanation in your reply and new handoff. Do not call the two
explanations consistent. Preserve finalized historical records unchanged.

For ordinary continue with a foreign owner, stop after inspect and give one
short conflict response. Do not call enter merely to demonstrate its refusal,
scan files, run shell commands, or open a blocking question card. A fresh
foreign owner needs handoff in the original tool; never offer forced takeover,
including as a future option in that response. A stale foreign owner needs
confirmation that the previous tool stopped modifying files and an explicit
takeover request; say this without automatically launching the takeover flow.
Lease freshness and timestamps do not prove a tool is running or stopped; do
not speculate about physical activity or elapsed time. These conflict replies
need only the blocking fact and the next safe user action, not a project report.

Before finalizing, reconcile the semantic summary with the verification entries
and actual command results. Test-run counts, commands and outcomes must agree
across the summary, evidence and reply. Prefer one factual verification entry
per execution; do not duplicate its full report in every semantic field. Omit a
test-run count if it cannot be verified, rather than estimating it.

Keep an ordinary status or handoff reply concise: the result, relevant pending
work and relevant unrun verification. Do not enumerate speculative new scope
or explain internal ownership in a successful reply. Use “可以换工具继续” only
after release is confirmed. A repeated completed handoff needs only a brief
confirmation; do not repeat the full prior report.

When verified pending work is empty and there is no unresolved blocker, ordinary
continue or status needs only a short completion statement and the relevant
verification result. Do not open a blocking scope-selection question or invent
new options merely because the backlog is empty. The user can provide a new
request later; retain ownership and finish this reply. If an existing pending
step really needs a decision, ask that specific question before dependent work.

For an ordinary success reply, use at most three short sentences unless the user
asks for detail. Relevant unrun verification means checks required by the agreed
task, not hypothetical extensions or a repeated list of unsupported platforms.
Preserve pertinent limitations in the handoff record, but do not repeat the whole
record in the reply. After a confirmed repeated handoff, one short confirmation
is sufficient unless a new problem changes what the user must do.

Use these examples only after replacing brackets with observed facts; never
invent an empty backlog or claim all previous changes are intact:

- Continue: `已接上：已完成[事实]，还需[待办]。接下来先[第一步]。`
- Handoff: `交接完成，现在可以换工具继续。`
- No pending work, after verification: `当前任务已完成，测试 [实际结果]。目前没有待办。`
- Confirmed repeated handoff: `已经交接完成，可以换工具继续。`
- Takeover: `已完成接管；交接上下文可能不完整。已核实[事实]，接下来先[恢复步骤]。`

If no handoff exists, say “未发现交接记录，将先检查项目现状。” A first handoff
reply must also summarize real pending work and task-required unrun verification,
if any.

Before sending an ordinary successful response, ask:

> Does this answer expose protocol implementation details that the user does not need?

If yes, rewrite it in product-level language.

## Ownership lifetime

Completing a task is **NOT** a handoff trigger. Successful verification is
**NOT** a handoff trigger. Having no pending work is **NOT** a handoff trigger.
Finishing your answer, status questions and idle time do not authorize release.
Ordinary successful work **MUST** retain the current workspace ownership.
Only explicit user handoff, release, or recovery intent that authorizes release
permits `workspace_leave`. Do not clean up ownership automatically.

## Continue or resume

Call `workspace_inspect`, then follow exactly one state transition:

| State | Action |
| --- | --- |
| no owner | call `workspace_enter` with `harness_type="claude-code"`, this real Claude session as `harness_instance_id`, and an explicit TTL; recover the latest handoff |
| current Claude instance, fresh | call `workspace_heartbeat` when appropriate, then continue |
| foreign owner, fresh | do not enter and do not take over; a fresh owner cannot be taken over; report the conflict |
| foreign owner, stale | ordinary enter is still refused; stale ownership does not disappear automatically; ask for explicit user takeover intent |
| current Claude instance, stale | it cannot be revived with `workspace_heartbeat`; ordinary continue does not authorize release. Explicit handoff or recovery intent authorizing release is required before `workspace_leave`; then `workspace_enter` only if continued work was requested |

Ordinary continue never implies takeover.

After successful acquisition, read `pending_work`, `next_steps`, decisions,
blockers and unrun verification. Check relevant files and execute the first
supported pending step without asking the user to restate the project. Missing
or conflicting facts stay unknown until verified. Expiry does not prove the
previous tool stopped modifying files; confirm it stopped before takeover.

## Hand off

For ordinary inspect, enter, leave and takeover calls, request
`context_mode="compact"` (default character budget 8000). This bounds the
returned historical handoff, not the record saved by leave. Read `context_info`
before continuing: if `requires_full_context` is true, call `workspace_inspect`
with `context_mode="full"` before development. Fetch omitted evidence in full
before relying on it. Missing or omitted pending work is unknown, not empty.
Historical verification is recorded by the previous session, not executed now.
Do not reconstruct omitted descriptions or treat them as new authorized work.

Inspect first and verify that this exact Claude instance owns the workspace.
Build semantic context and objective evidence only from observed work. Call
`workspace_leave` once; it finalizes the handoff, releases ownership, and ends
the Core session. Do not invent files, commands, or test results and do not
repeat the component operations separately. Preserve actionable pending work,
decisions, blockers and the first next step; label verification not performed
as unrun. Only report handoff success after `workspace_leave` succeeds.

On failure, inspect actual state: persistence failure is not saved; saved
context with release failure is not yet released. Do not invite another tool to
modify before release. Retry a known prior leave with its original session and
lease identifiers and context, including after a lost response. Existing
finalized context is reused, never rewritten. A repeated handoff must not create
or acquire a new session. If no prior result or identifiers are available,
report what can be verified. If takeover committed but context loading failed,
retain the returned ownership identifiers and recover context; do not retry takeover.

## Explicit takeover

Only explicit user language such as "接管" or "take over" authorizes this flow.
Inspect again, verify that the foreign owner is stale, and call
`workspace_takeover` using the observed lease identifier. Treat recovered
context as possibly incomplete. A fresh owner cannot be taken over.

## Before mutation

The PreToolUse guard protects Claude Code's `Write`, `Edit`, `NotebookEdit`,
`Bash`, `PowerShell`, `EnterWorktree`, `ExitWorktree`, and `Task` tools. Do not work around a
denial. MCP workflow tools remain unguarded so the assistant can recover safely.

Before allowing a protected call, the guard refreshes this exact fresh owner's
lease using its existing TTL. It never revives stale ownership or creates a
session. There is no timer during a long-running tool or idle period; maintain
heartbeat between long steps while still fresh, and stop after expiry. Expiry
does not stop an already running command, so never infer that it is safe to
take over solely from expiry.

- Write and Edit use `tool_input.file_path` and are checked against the target
  workspace.
- NotebookEdit uses `tool_input.notebook_path`, never `file_path`.
- Bash and PowerShell are checked only against hook cwd. The guard does not parse their commands,
  so this is participating-harness enforcement rather than a filesystem lock.
- EnterWorktree is denied from a managed workspace even for its fresh owner,
  because the new checkout would not inherit the workspace identity.
- Task with `isolation="worktree"` follows the same rule. Ordinary Task is not
  blocked at the outer layer; subagent mutations still encounter normal hooks.
  `agent_id` does not replace the Claude session identity.
- ExitWorktree from a managed cwd requires this Claude instance to be the fresh
  owner. It is allowed from an unmanaged cwd.

The denial reason identifies the valid recovery path. Follow it without copying
its internal IDs or protocol narration into the ordinary final response.

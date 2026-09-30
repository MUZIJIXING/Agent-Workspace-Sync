---
name: agent-workspace-sync
description: Coordinate managed workspaces for ZCode. Ordinary completed work keeps ownership; only explicit current-user handoff/release intent permits leaving. Use for continue, resume, or start working ("继续", "接着做", "继续这个项目", "resume", "continue working"), handoff or stop ("交接一下", "我先走了", "handoff", "hand off"), status ("现在什么情况", "谁在用", "workspace status"), explicit takeover ("接管", "take over"), and before modifying an initialized Agent Workspace Sync project.
---

# Agent Workspace Sync for ZCode

This project may be managed by Agent Workspace Sync, which allows only one
coding harness to develop a workspace at a time. This ZCode session is one
harness instance:

```text
harness_type        = "zcode"
harness_instance_id = <the ZCode session id from the SessionStart context>
```

Use the Agent Workspace Sync MCP tools (`workspace_inspect`,
`workspace_enter`, `workspace_heartbeat`, `workspace_leave`,
`workspace_takeover`). Never edit ownership state by hand, and never take over
another owner on your own initiative.

## Prerequisite

The `agent-workspace-sync` package and its MCP extra must be installed in the
Python environment selected for both MCP and hooks. From the source checkout,
use the documented project-local virtual environment and `setup --client zcode`
to generate a bundle bound to that interpreter. Run `doctor` with the same
interpreter before loading the bundle. Do not install into a global environment
or silently change client settings.

The plugin calls everything as Python modules, so the console scripts do **not**
have to be on PATH. Installing is a user step: the plugin and its hooks never
install anything, and if Python or the package is missing the MCP server and the
hooks simply fail to start.

## Final-response contract — hide the internal protocol

This contract applies to the assistant's **final natural-language response**.
It does not suppress ZCode's native tool-call UI, which may visibly render tool
labels such as `workspace_inspect` or `workspace_enter`. Tool calls and their
structured results still carry the full internal data.

For an ordinary successful flow, the final natural-language response
**MUST NOT** mention or narrate any of the following:

- `workspace_inspect`, `workspace_enter`, `workspace_leave`,
  `workspace_takeover`, or `workspace_heartbeat`;
- MCP;
- `session`, `session_id`, `lease`, `lease_id`, lease freshness, lease expiry,
  or an expiry / expiration timestamp;
- `handoff_id` or `workspace_id`;
- internal protocol steps such as inspecting, entering, acquiring or holding
  ownership, refreshing it, retiring it, or retaining it.

**NEVER** append ownership, lease, session, expiry, identifier, tool-call, or
protocol details to an ordinary successful reply. Say only what happened in
product-level language that helps the user continue their work.

There are exactly two exceptions:

1. the user explicitly requests diagnostics, internal state, or identifiers;
2. recovery is blocked and the user must take an explicit safety action, such
   as deciding whether to take over a stale foreign owner.

Even in the second exception, prefer product language and omit identifiers:

Bad:

```text
workspace_takeover requires lease_id lea_xxx.
```

Good:

```text
之前的占用已经失效。如果你要继续，需要你明确确认接管这个工作区。
```

Expose an identifier only when it is genuinely required for manual recovery.

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

- Continue / resume:

  ```text
  已接上：已完成[事实]，还需[待办]。接下来先[第一步]。
  ```

- Handoff:

  ```text
  交接完成，现在可以换工具继续。
  ```

- Takeover:

  ```text
  已完成接管；交接上下文可能不完整。已核实[事实]，接下来先[恢复步骤]。
  ```

- No pending work, after verification: `当前任务已完成，测试 [实际结果]。目前没有待办。`
- Confirmed repeated handoff: `已经交接完成，可以换工具继续。`

If no handoff exists, say “未发现交接记录，将先检查项目现状。” A first handoff
reply must also summarize real pending work and task-required unrun verification,
if any.

On a successful enter before the first requested work, normally say nothing
about entering or ownership; report only the result of the requested work.

Before sending an ordinary success response, ask:

> Does this answer expose protocol implementation details that the user does not need?

If yes, rewrite it in product-level language before sending it.

## Ownership lifetime

For ordinary inspect, enter, leave and takeover calls, request
`context_mode="compact"` (default character budget 8000). This bounds the
returned historical handoff, not the record saved by leave. Read `context_info`
before continuing: if `requires_full_context` is true, call `workspace_inspect`
with `context_mode="full"` before development. Fetch omitted evidence in full
before relying on it. Missing or omitted pending work is unknown, not empty.
Historical verification is recorded by the previous session, not executed now.
Do not reconstruct omitted descriptions or treat them as new authorized work.

Completing a task is **NOT** a handoff trigger. Successful verification is
**NOT** a handoff trigger. Having no pending work is **NOT** a handoff trigger.
Finishing your current answer is **NOT** a handoff trigger. Ordinary successful
work **MUST** retain the current workspace ownership.

**NEVER** call `workspace_leave` merely because the requested work is complete.
**NEVER** proactively clean up, finalize, hand off, or release workspace
ownership. `workspace_leave` is appropriate only when the **CURRENT USER
MESSAGE** explicitly asks to hand off, release, or leave the workspace. An
earlier conversation mention, inferred future collaboration, a belief that it
would help another harness, task completion, successful verification, no
pending work, or an idle state does not authorize it.

An explicit recovery request may also authorize releasing current stale
ownership, but ordinary continue does not authorize release. Explain this
recovery action and obtain that intent before proceeding.

The normal lifecycle is:

```text
user asks for work
→ obtain or retain ownership
→ perform the work
→ verify it
→ report the result → KEEP ownership
```

Only an explicit handoff request adds a handoff and release step:

```text
“交接一下”
→ truthful handoff
→ workspace_leave
```

Examples:

- User: “把 A 改成 B”
  - Correct: modify → verify → report success → retain ownership.
  - Incorrect: modify → verify → `workspace_leave`.
- User: “交接一下”
  - Correct: truthful handoff → `workspace_leave` → report that the workspace
    is released.
- User: “完成了吗？”
  - Correct: report status → retain ownership.
  - Incorrect: interpret task completion as permission to release.

## Continue / resume — "继续", "接着做", "resume"

1. Call `workspace_inspect`.

2. **No owner** → call `workspace_enter` with `harness_type="zcode"`,
   `harness_instance_id=<current ZCode session id>`, and an explicit
   `ttl_seconds` (3600 is a reasonable starting point). Then read the returned
   `latest_handoff` and carry on with the user's task.

3. **Owner is this ZCode session and the lease is fresh** → this session already
   holds the workspace. Call `workspace_heartbeat` to refresh the TTL and
   continue. Do **not** call `workspace_enter` again.

4. **Owner is another harness or another ZCode session, lease fresh** → do not modify the
   workspace. Say in product language that another active harness currently
   owns it, without exposing instance, freshness, expiry, or identifiers unless
   the user explicitly requests diagnostics.

5. **Foreign owner's lease is stale** → ordinary "continue" still must **not** take
   over. Explain that expiry does not prove the previous tool stopped modifying
   files; ask them to explicitly confirm takeover after confirming it stopped.
   Do not narrate the
   lease, its identifier, or its expiry. Wait for the user to say "接管" / "take
   over".

6. **This instance's lease is stale** → stop mutations. Ordinary continue does
   not authorize release. Ask for explicit handoff or recovery intent authorizing
   release before `workspace_leave`; enter again only if continued work was requested.

After successful acquisition, read `pending_work`, `next_steps`, decisions,
blockers, and unrun verification. Check relevant files, then execute the first
supported pending step without asking the user to restate the project. Missing
or conflicting facts stay unknown until verified. No handoff means no claimed
restoration of a previous session.

## Hand off / leave — "交接一下", "我先走了", "hand off"

1. Call `workspace_inspect` and confirm the active owner is this ZCode session
   (`harness_type="zcode"` and `harness_instance_id=<current session id>`). If it
   is not, say so and stop; do not hand off someone else's workspace.

2. Build the handoff from what actually happened in this session:

   - `semantic_context`: `goal`, `progress_summary`, `completed_work`,
     `pending_work`, `decisions`, `blockers`, `next_steps`,
     `constraints_or_warnings` — include the ones that apply.
   - `objective_evidence`: `changed_files`, version-control state, tests run,
     test results, verification, important commands and their results.

   Only record work that actually happened. Never invent test runs, test
   results, or version-control state you did not observe.

   Preserve actionable pending work, the first next step, decisions and blockers.
   Label verification not performed as unrun; distinguish observed evidence from
   previous Agent claims.

3. Call `workspace_leave` with the session id, lease id, semantic
   context, and objective evidence. It finalizes the handoff, releases
   ownership, and ends the session.

4. Only after success, use the handoff success template from the final-response contract,
   without appending protocol details. Do **not** additionally call release,
   session end, or handoff finalize — `workspace_leave` already covers them.

On failure, do not claim handoff completion or invite another tool to modify.
Inspect the actual state: persistence failure must not be described as saved;
saved context with release failure must be described as not yet released.
Retry a known prior leave with its original session and lease identifiers and
context, including after a lost response. Existing finalized context is reused,
never rewritten. A repeated handoff must not create or acquire a new session.
If no prior result or identifiers are available, report what can be verified.
If takeover committed but context loading failed, retain the returned ownership
identifiers and recover context; do not retry takeover.

## Status — "现在什么情况", "谁在用", "workspace status"

Call `workspace_inspect` and summarize: workspace identity, owning session and
harness, lease freshness and expiry, and the latest finalized handoff. This is
read-only; change nothing.

## Explicit takeover — "接管", "take over", "强制接手"

Only when the user explicitly asks:

1. `workspace_inspect` first.
2. Confirm the active lease is actually stale, and take its `lease_id` as
   `expected_previous_lease_id`.
3. Call `workspace_takeover` with `harness_type="zcode"`,
   `harness_instance_id=<current session id>`, the observed
   `expected_previous_lease_id`, and an explicit `ttl_seconds`.
4. Read the returned `latest_handoff`. Treat it as possibly incomplete: the
   previous owner did not finish a normal handover.

A fresh owner is never taken over, and ordinary "continue"/"resume" never
triggers a takeover.

## Before modifying code

If this workspace is managed by Agent Workspace Sync, make sure this session
holds a fresh lease before writing, editing, or running shell commands. The
plugin's PreToolUse guard enforces this for ZCode's own Write, Edit, and Bash
tools.

Before allowing a protected call, the guard refreshes this exact fresh owner's
lease using its existing TTL. It never revives stale ownership or creates a
session. There is no timer during a long-running tool or idle period; maintain
heartbeat between long steps while still fresh, and stop after expiry. Expiry
does not stop an already running command, so never infer that it is safe to
take over solely from expiry.

Enforcement is workspace-scoped, and which workspace that is depends on the
tool:

- **Write / Edit — target-workspace enforcement.** The guard judges the
  workspace that holds `tool_input.file_path`, because that is the file being
  changed. A write started from an unmanaged directory into a managed workspace
  is still governed by that managed workspace, and a write started from your own
  workspace into someone else's is still denied. If the target path is missing,
  empty, not a string, or cannot be resolved, the guard denies the call rather
  than guessing.
- **Bash — current-working-directory enforcement.** A shell command's real
  mutation targets cannot be identified safely (`echo x > /other/project/a.txt`,
  `python writes_anywhere.py`), so the guard judges the workspace of the hook
  cwd and does not attempt to parse the command.

In both cases: no owner, a different owner, or a stale lease → the tool call is
denied. This session owning a fresh lease on the relevant workspace → it is
allowed. When a call is denied, do not try to work around it by other means;
report the ownership state and ask the user what to do.

### Ownership states and the only correct action

| State | What to do |
| --- | --- |
| no active owner | `workspace_enter` with this session's identity |
| this ZCode session owns it, lease fresh | `workspace_heartbeat` |
| a foreign owner, lease fresh | do not enter and do not take over — a fresh owner can never be taken over. Report the ownership conflict and stop. |
| a foreign owner, lease lapsed (stale) | ordinary `workspace_enter` is still refused, and the lease does not disappear by itself. Only an explicit user takeover may replace that owner: `workspace_inspect`, then `workspace_takeover` with the lease id observed there. |
| this ZCode session owns it, lease lapsed | `workspace_heartbeat` cannot revive a lapsed lease, and a session cannot take over from itself. Ordinary continue does not authorize release. Explicit handoff or recovery intent authorizing release is required before `workspace_leave`; then `workspace_enter` only if continued work was requested. |

Two readings of a lapsed lease are wrong and must never appear in your replies
or your reasoning:

* "wait until it goes away and then a normal enter will work" — Core keeps the
  stale row `active` and keeps refusing ordinary acquisition until the owner
  releases it or an explicit takeover retires it;
* "a fresh owner can be forced over" — a fresh owner cannot be displaced at all.

The guard's denial reason already tells you which of these states you are in and
names the next call; follow it instead of guessing.

### Known bypass

A Bash command launched from outside a managed workspace can still explicitly
mutate a managed workspace path. This phase performs no shell-command semantic
analysis and no OS-level filesystem enforcement, so the guarantee is
participating-harness enforcement — ZCode's own Write, Edit, and Bash tools —
not a filesystem lock. The same applies to anything that does not go through
these tools at all.

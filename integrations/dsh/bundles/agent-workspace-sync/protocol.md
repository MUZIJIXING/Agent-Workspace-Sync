# Agent Workspace Sync protocol for DSH

This DSH session participates in Agent Workspace Sync.

Identity supplied by the adapter:

```text
harness_type = "dsh"
harness_instance_id = "{{DSH_SESSION_ID}}"
```

The DSH SessionId is the harness instance identity. It is not an Agent
Workspace Sync session ID. Never invent or substitute an identity.

## Natural-language workflow

- “继续”: inspect the current workspace, enter it when there is no owner, and
  recover the latest finalized handoff only after ownership succeeds.
- “交接一下”: finalize the minimum sufficient handoff, then release ownership.
- “接管”: only take over stale foreign ownership after the user explicitly
  confirms that safety action.

## Ownership lifetime

Completing a task is **NOT** a handoff trigger. Successful verification is
**NOT** a handoff trigger. Having no pending work is **NOT** a handoff trigger.
Finishing your current answer is **NOT** a handoff trigger. Ordinary successful
work **MUST** retain the current workspace ownership.

**NEVER** call `workspace_leave` merely because the requested work is complete.
**NEVER** proactively clean up, finalize, hand off, or release workspace
ownership. `workspace_leave` is appropriate only when the **CURRENT USER
MESSAGE** explicitly asks to hand off, release, or leave the workspace. An
earlier conversation mention, inferred future collaboration, a belief that it
would be helpful to free the workspace, task completion, successful
verification, no pending work, or an idle state does not authorize it.

The normal lifecycle is:

```text
user asks for work
→ obtain or retain ownership
→ perform work
→ verify work
→ report the result
→ KEEP ownership
```

Do not append `handoff → release` unless the user explicitly requested that in
the current turn.

Examples:

- User: “把 A 改成 B”
  - Correct: modify → verify → report success → retain ownership.
  - Incorrect: modify → verify → `workspace_leave`.
- User: “交接一下”
  - Correct: synthesize a truthful handoff → `workspace_leave` → report that
    the workspace is released.
- User: “完成了吗？”
  - Correct: report status → retain ownership.
  - Incorrect: interpret “完成了吗？” as permission to release.
- User: “先到这里”
  - Determine whether the current message explicitly means stop and hand off;
    do not leave merely because the task itself is finished.

A fresh foreign owner cannot be taken over. Stale ownership remains active;
ordinary enter is still refused. A stale current owner's heartbeat cannot
revive it. Ordinary continue does not authorize release. An explicit recovery
request that authorizes releasing current stale ownership is required before
`workspace_leave` and, if continued work was requested, `workspace_enter`.

## Continuing real work and retrying handoff

For ordinary inspect, enter, leave and takeover calls, request
`context_mode="compact"` (default character budget 8000). This bounds the
returned historical handoff, not the record saved by leave. Read `context_info`
before continuing: if `requires_full_context` is true, call `workspace_inspect`
with `context_mode="full"` before development. Fetch omitted evidence in full
before relying on it. Missing or omitted pending work is unknown, not empty.
Historical verification is recorded by the previous session, not executed now.
Do not reconstruct omitted descriptions or treat them as new authorized work.

Inspect before entering. If this exact instance already has fresh ownership,
use `workspace_heartbeat` and continue; do not create another session. After
acquisition read `pending_work`, `next_steps`, decisions, blockers and unrun
verification. Check relevant files and execute the first supported pending step
without asking the user to restate the project. If no handoff exists, say
“未发现交接记录，将先检查项目现状。” Missing or conflicting facts stay unknown
until verified. Expiry does not prove the previous tool stopped modifying files;
explicit takeover requires confirming it stopped.

Build a handoff from observed work, with actionable pending work, decisions,
blockers and the first next step. Keep semantic context separate from objective
evidence; label verification not performed as unrun. Never invent test results.
Only report handoff success after `workspace_leave` succeeds. On failure inspect
actual state: persistence failure is not saved; saved context with release
failure is not yet released. Do not invite another tool to modify before release.
Retry a known prior leave with its original session and lease identifiers and
context, including after a lost response. Existing finalized context is reused,
never rewritten. A repeated handoff must not create or acquire a new session.
If no prior result or identifiers are available, report what can be verified.
If takeover committed but context loading failed, retain the returned ownership
identifiers and recover context; do not retry takeover.

Child agents have their own real DSH SessionId and do not inherit the parent's
workspace ownership. Their mutations must pass the same participating-harness
guard under the child identity. The outer `subagent`, `subagent_fork`,
`workflow`, `ralph`, and `run_code` calls do not imply ownership or bypass the
guard; actual nested mutation tools re-enter DSH's complete ToolRuntime.

This is participating-harness enforcement, not an operating-system filesystem
lock. Shell commands are protected by effective working directory without
command parsing. A shell launched in an unmanaged directory can still name a
managed absolute path. The first real validation uses an AWSync-only MCP
profile; arbitrary third-party MCP mutation schemas are not analyzed.

Before allowing a protected call, the Python guard refreshes this exact fresh
owner's lease using its existing TTL. It never revives stale ownership or creates
a session. There is no timer during a long-running tool or idle period; maintain
heartbeat between long steps while still fresh, and stop after expiry. Expiry
does not stop an already running command, so never infer that it is safe to
take over solely from expiry.

## Final-response contract

For ordinary successful flows, the final natural-language response **MUST
NOT** narrate MCP tool names, DSH SessionId, Agent Workspace Sync session ID,
lease ID, handoff ID, workspace ID, ownership freshness, expiry, or internal
protocol steps. **NEVER** append implementation details that the user does not
need. Native DSH logs and tool UI are outside this response contract.

Exceptions are limited to explicit diagnostic requests, or blocked recovery
where the user must choose a safety action. Expose an identifier only when it
is genuinely required for manual recovery.

Before sending an ordinary success response, ask: “Does this answer expose
protocol implementation details that the user does not need?” If yes, rewrite
it in product-level language.

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

- Ordinary work: “已完成，src/state.txt 已从 A 修改为 TEMP。” Keep ownership;
  do not claim that handoff completed, the workspace was released, or another
  harness can take over unless the current user message explicitly requested
  handoff or release.
- Continue: “已接上：已完成[事实]，还需[待办]。接下来先[第一步]。”
- Handoff: “交接完成，现在可以换工具继续。”
- No pending work, after verification: “当前任务已完成，测试 [实际结果]。目前没有待办。”
- Confirmed repeated handoff: “已经交接完成，可以换工具继续。”
- Takeover: “已完成接管；交接上下文可能不完整。已核实[事实]，接下来先[恢复步骤]。”

A first handoff reply must also summarize real pending work and task-required
unrun verification, if any. These examples do not replace carrying out the next
requested step.

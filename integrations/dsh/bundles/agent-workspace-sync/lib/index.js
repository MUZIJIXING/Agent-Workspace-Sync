import { spawnSync as nodeSpawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

export const name = "agent-workspace-sync-dsh-adapter";
export const inject = ["tools"];

const BRIDGE_TIMEOUT_MS = 5000;
const BRIDGE_FAILURE =
  "Agent Workspace Sync protection could not be verified, so this operation was denied.";
const IDENTITY_FAILURE =
  "Agent Workspace Sync could not verify the real DSH session identity, so this operation was denied.";

const FILE_TOOLS = new Set(["write", "edit"]);
const SHELL_TOOLS = new Set(["pwsh", "bash"]);
const STR_REPLACE_MUTATIONS = new Set(["create", "str_replace", "insert"]);
const DENY_CATEGORIES = new Set([
  "NO_OWNER",
  "FOREIGN_FRESH_OWNER",
  "FOREIGN_STALE_OWNER",
  "CURRENT_OWNER_STALE",
  "INVALID_STATE",
]);
const RECOVERY_TOOLS = new Set([
  "mcp__agent-workspace-sync__workspace_inspect",
  "mcp__agent-workspace-sync__workspace_enter",
  "mcp__agent-workspace-sync__workspace_heartbeat",
  "mcp__agent-workspace-sync__workspace_leave",
  "mcp__agent-workspace-sync__workspace_takeover",
]);

const protocolTemplate = readFileSync(
  new URL("../protocol.md", import.meta.url),
  "utf8",
);

export function resolveSessionIdentity(agent) {
  const candidates = [
    ["agent.id", agent?.id],
    ["agent.session.id", agent?.session?.id],
    ["agent.session.header.id", agent?.session?.header?.id],
  ];
  const headerId = agent?.session?.header?.id;
  if (typeof headerId !== "string" || headerId.trim() === "") {
    return { ok: false, reason: "agent.session.header.id is missing" };
  }
  const present = candidates.filter(([, value]) => value !== undefined && value !== null);
  const malformed = present.find(
    ([, value]) => typeof value !== "string" || value.trim() === "",
  );
  if (malformed) {
    return { ok: false, reason: `${malformed[0]} is malformed` };
  }
  const distinct = new Set(present.map(([, value]) => value));
  if (distinct.size !== 1) {
    return {
      ok: false,
      reason: "agent.id, agent.session.id, and agent.session.header.id disagree",
    };
  }
  return { ok: true, sessionId: headerId };
}

function protocolContext(sessionId) {
  return protocolTemplate.replaceAll("{{DSH_SESSION_ID}}", sessionId);
}

function injectedMessage(text) {
  return Object.freeze({
    id: randomUUID(),
    role: "user",
    content: Object.freeze([Object.freeze({ type: "text", text })]),
    source: Object.freeze({ kind: "plugin", plugin: "agent-workspace-sync" }),
  });
}

function classifyExecution(exec) {
  if (RECOVERY_TOOLS.has(exec?.name)) return "allow";
  if (FILE_TOOLS.has(exec?.name) || SHELL_TOOLS.has(exec?.name)) return "guard";
  if (exec?.name !== "str_replace_editor") return "allow";
  const command = exec?.arguments?.command;
  if (command === "view") return "allow";
  if (STR_REPLACE_MUTATIONS.has(command)) return "guard";
  return "deny";
}

function validBridgeDecision(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  if (value.decision === "allow") {
    return keys.length === 1 && keys[0] === "decision";
  }
  if (value.decision === "deny") {
    return (
      keys.join(",") === "category,decision,reason" &&
      typeof value.reason === "string" &&
      value.reason.trim() !== "" &&
      typeof value.category === "string" &&
      DENY_CATEGORIES.has(value.category)
    );
  }
  return false;
}

export function invokePythonBridge(payload, options = {}) {
  const python = options.python ?? process.env.AWSYNC_PYTHON;
  const spawn = options.spawnSync ?? nodeSpawnSync;
  if (typeof python !== "string" || python.trim() === "") {
    console.error("agent-workspace-sync: AWSYNC_PYTHON is missing");
    return { decision: "deny", reason: BRIDGE_FAILURE };
  }

  let completed;
  try {
    completed = spawn(
      python,
      ["-m", "agent_workspace_sync.dsh_hook", "guard-tool"],
      {
        input: JSON.stringify(payload),
        encoding: "utf8",
        shell: false,
        timeout: BRIDGE_TIMEOUT_MS,
      },
    );
  } catch (error) {
    console.error("agent-workspace-sync: Python guard spawn threw", error);
    return { decision: "deny", reason: BRIDGE_FAILURE };
  }

  if (completed?.stderr) console.error(String(completed.stderr).trimEnd());
  if (completed?.error || completed?.signal || completed?.status !== 0) {
    console.error("agent-workspace-sync: Python guard did not complete successfully");
    return { decision: "deny", reason: BRIDGE_FAILURE };
  }

  let result;
  try {
    result = JSON.parse(completed.stdout);
  } catch (error) {
    console.error("agent-workspace-sync: Python guard returned malformed JSON", error);
    return { decision: "deny", reason: BRIDGE_FAILURE };
  }
  if (!validBridgeDecision(result)) {
    console.error("agent-workspace-sync: Python guard returned an unexpected decision");
    return { decision: "deny", reason: BRIDGE_FAILURE };
  }
  return result;
}

export function guardExecution(exec, bridge = invokePythonBridge) {
  const classification = classifyExecution(exec);
  if (classification === "allow") return undefined;
  if (classification === "deny") return BRIDGE_FAILURE;

  const identity = resolveSessionIdentity(exec?.agent);
  if (!identity.ok) {
    console.error(`agent-workspace-sync: ${identity.reason}`);
    return IDENTITY_FAILURE;
  }
  const result = bridge({
    session_id: identity.sessionId,
    cwd: exec?.agent?.session?.header?.cwd,
    tool_name: exec.name,
    arguments: exec.arguments,
  });
  return result?.decision === "allow" ? undefined : result?.reason ?? BRIDGE_FAILURE;
}

export function apply(ctx, config = {}) {
  const bridge = (payload) => invokePythonBridge(payload, { python: config.python });
  ctx.tools.guard((exec) => guardExecution(exec, bridge));
  ctx.on("agent/session-start", ({ agent }) => {
    const identity = resolveSessionIdentity(agent);
    if (identity.ok) {
      agent.inject(injectedMessage(protocolContext(identity.sessionId)));
      return;
    }
    const warning =
      `Agent Workspace Sync adapter identity warning: ${identity.reason}. ` +
      "Ownership-protected mutations will fail closed until the real DSH SessionId is consistent. " +
      "No replacement identity was generated.\n\n" +
      protocolContext("<unavailable: inconsistent DSH identity>");
    console.error(`agent-workspace-sync: ${identity.reason}`);
    agent.inject(injectedMessage(warning));
  });
}

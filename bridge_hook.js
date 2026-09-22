import fs from "node:fs";
import path from "node:path";
import cp from "node:child_process";

export async function handleCodexPrompt(sessionId, mode, content) {
  const textParts = content.filter(p => p.type === "text").map(p => p.text);
  const fullText = textParts.join("\n");
  const imgParts = content.filter(p => p.type === "image");
  const savedImgPaths = [];
  if (imgParts.length > 0) {
    const tmpDir = "/tmp/codex_dsh_uploads";
    fs.mkdirSync(tmpDir, { recursive: true });
    for (let i = 0; i < imgParts.length; i++) {
      const img = imgParts[i];
      const ext = img.mediaType ? img.mediaType.split("/")[1] || "png" : "png";
      const imgFile = path.join(tmpDir, `upload_${Date.now()}_${i}.${ext}`);
      fs.writeFileSync(imgFile, Buffer.from(img.data, "base64"));
      savedImgPaths.push(imgFile);
    }
  }
  const bridgePayload = JSON.stringify({
    text: fullText,
    images: savedImgPaths,
    mode: mode || "followup"
  });
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "prompt",
    sessionId,
    bridgePayload
  ], { encoding: "utf-8" });

  let parsed;
  try {
    parsed = JSON.parse((res.stdout || "").trim());
  } catch (e) {
    parsed = { ok: false, error: res.stderr || res.stdout || String(e) };
  }
  return parsed;
}

export function handleCodexArchive(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "archive",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexRename(sessionId, title) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "rename",
    sessionId,
    title
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexCreate(workspacePath) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "create",
    workspacePath || "None"
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexFork(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "fork",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

export function handleCodexCancel(sessionId) {
  cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "cancel",
    sessionId
  ]);
}

/**
 * Push the web UI's model choice through to Codex for this thread.
 *
 * The web picker is DSH-local state: without this the choice only changed the
 * label in the browser while Codex kept answering with the model from its own
 * config.toml. Codex takes provider-qualified ids verbatim, so the DSH catalog
 * id is forwarded unchanged.
 */
export function handleCodexModel(sessionId, model, reasoningEffort) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "model",
    sessionId,
    model,
    reasoningEffort || ""
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

/**
 * Read the model this thread is on without asking DSH to resume it.
 *
 * session.models resolves the session's agent, and this edition's session store
 * is the projected log, so the stock read appends a second writer's events to
 * it and the browser rejects the log as corrupt. The recorded choice is the
 * same answer and costs nothing.
 */
export function handleCodexModelState(sessionId) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "model-state",
    sessionId
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

/** Switch this thread's sandbox and approval preset through Codex. */
export function handleCodexPermission(sessionId, preset) {
  const res = cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "permission",
    sessionId,
    preset
  ], { encoding: "utf-8" });
  try {
    return JSON.parse((res.stdout || "").trim());
  } catch (e) {
    return { ok: false, error: res.stderr || res.stdout || String(e) };
  }
}

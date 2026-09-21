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
  cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "archive",
    sessionId
  ]);
}

export function handleCodexRename(sessionId, title) {
  cp.spawnSync("python3", [
    "/home/nahida/agents/sever/dsh/codex_bridge.py",
    "rename",
    sessionId,
    title
  ]);
}


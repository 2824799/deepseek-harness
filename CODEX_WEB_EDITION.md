# Codex Web Edition

The stock DSH web UI driven by Codex Desktop instead of the DSH-local agent.
Every tool call, model response, and session record belongs to Codex; the web
page is only a view and an input surface for it.

## Layout

| Piece | Path | Role |
| --- | --- | --- |
| Web UI | `127.0.0.1:3080` | The DSH front end, isolated on its own port |
| State | `.dsh-codex/` | This edition's own DSH home; `~/.dsh` is never touched |
| App-server | `ws://127.0.0.1:45880` | Codex JSON-RPC channel the bridge talks to |
| Bridge | `codex_link.py`, `codex_bridge.py` | Executes web actions through Codex |
| Projection | `setup_isolated.py` | Turns Codex threads into DSH session logs |
| Live push | `codex_tailer.js` | Streams new Codex events into the browser |
| Patching | `patch_apiproxy.js` | Idempotently installs the host hooks |

## How a message travels

1. The page POSTs `session.prompt`; `patch_apiproxy.js` intercepts it.
2. `codex_bridge.py` attaches to the thread and sends it with `turn/start`,
   or `turn/steer` when a turn is already running.
3. When Codex Desktop holds the thread's writer lock, the message goes to the
   durable `codex queue` instead, which Desktop drains.
4. `setup_isolated.py` re-projects the thread; `codex_tailer.js` pushes the new
   events over the mux WebSocket so the page updates without a reload.

## Services

```
systemctl --user status codex-dsh-appserver.service   # Codex app-server (45880)
systemctl --user status codex-dsh-web.service         # DSH web UI (3080)
systemctl --user status codex-dsh-sync.service        # projection daemon
```

## Reapplying after a DSH upgrade

```
node /home/nahida/agents/sever/dsh/patch_apiproxy.js
systemctl --user restart codex-dsh-web.service
```

The patcher is idempotent and reports a `MISS` for any hook whose anchor moved.

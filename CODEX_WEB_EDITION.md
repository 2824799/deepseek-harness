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
| Streaming | `codex_stream.py` | Follows a live turn's token deltas into that log |
| Live push | `codex_tailer.js` | Streams new Codex events into the browser |
| Patching | `patch_apiproxy.js` | Idempotently installs the host hooks |

## What the web controls

| Surface | How it reaches Codex |
| --- | --- |
| Send a message | `session.prompt` → `turn/start`, or `turn/steer` mid-turn |
| Stop | `session.cancel` → `turn/interrupt` |
| Model + reasoning effort | recorded per thread, sent on every `turn/start` |
| Permission preset | recorded per thread, sent as `sandboxPolicy` + `approvalPolicy` |
| Rename | `thread/name/set` |
| Archive / unarchive | `thread/archive`, with a state-table fallback for open threads |
| Fork | `thread/fork` |
| New conversation | `thread/start` |
| Images | forwarded as `image` input parts |

Every one of these writes to Codex only. The DSH session log is a read model the
projector owns, so no control path may resume a DSH agent: a second writer would
collide with the projector's sequence numbers and the browser would reject the
whole conversation as corrupt.

## Live conversation

`session/event` frames reach the page over the mux WebSocket. The projector
polls Codex every 700 ms, and `codex_tailer.js` follows the projected files by
offset, so a reply appears within roughly a second. Token-level output needs the
turn's own connection: Codex emits deltas only on the socket that submitted the
turn, so `codex_stream.py` inherits that socket in a detached child and appends
the deltas under the log's lock while the projector writes the structure around
them.

Reasoning text is requested with `summary: "detailed"`; without it Codex sends
the reasoning item empty and the thinking panel stays blank.

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

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
| Streaming | codex_stream.py and projection_writer.py | Forward live Codex deltas to one projection writer |
| Live push | `codex_tailer.js` | Streams new Codex events into the browser |
| Patching | `patch_apiproxy.js` | Idempotently installs the host hooks |

Workspace creation, rename, deletion, and ordering go through Codex's
experimental project APIs. Moving a conversation to a project updates its
Codex thread metadata. The browser never writes the projected workspace
registry or session logs. The sync daemon alone refreshes these read models
from Codex, including empty projects and project removals. Existing projected
workspace IDs are retained for paths still present in Codex.
Codex supports changing a conversation's project but has no manual
conversation-order API; a manual reorder request returns an error before it
changes project membership.

## What the web controls

| Surface | How it reaches Codex |
| --- | --- |
| Send a message | `session.prompt` → `turn/start`, or `turn/steer` mid-turn |
| Stop | `session.cancel` → `turn/interrupt` |
| Model + reasoning effort | recorded per thread, sent on every `turn/start` |
| Permission preset | recorded per thread, sent as `sandboxPolicy` + `approvalPolicy` |
| Rename | `thread/name/set` |
| Archive / unarchive | `thread/archive`, with a state-table fallback for open threads |
| Fork | Codex thread/fork for latest state; historical anchors return an explicit error |
| New conversation | `thread/start` |
| Images | forwarded as `image` input parts |

For a new conversation, the host resolves the selected workspace id to its
directory before starting a Codex thread. The pending control file records a
blank Codex thread's workspace. Only the sync daemon creates its projected
session header; a page opened before that sweep gets a temporary read-only
header from the pending control file. The sidebar hides the blank thread until
its first message is projected.

Conversation actions are sent to Codex. The projected session log is owned by
the sync daemon. A DSH agent must not resume it, because a second writer would
collide with the projector's sequence numbers.

## Live conversation

The sync daemon checks Codex threads every 700 ms and refreshes the project
list at most once every 3 seconds. It keeps per-rollout byte cursors
across sweeps and skips unchanged sessions, including those without SQLite
history items. The browser follows the projected JSONL by byte offset.

Codex sends token deltas on the socket that started a web turn. The detached
stream follower forwards these deltas through a local Unix socket. The sync
daemon writes them into the same projected log as the structural events.
Desktop-owned turns are read from completed rollout items; a completed
reasoning item is published immediately, even when the next tool or answer has
not arrived. An older checkpoint holding unpublished reasoning is drained on
the next sweep. A projected session remains listed while Codex's history index
lags behind its rollout, including an index with no rows for that thread.
Unchanged workspace, projection-cache and checkpoint files are not rewritten.

On the current dataset, an isolated warm sweep costs about 50 ms CPU, compared
with about 470 ms before these changes. The first sweep still parses backlog,
and the projected logs still use disk space. This benchmark does not establish
Codex Desktop's internal CPU use.

Reasoning text is requested with detailed summaries when the model exposes it.

## How a message travels

1. The page POSTs `session.prompt`; `patch_apiproxy.js` intercepts it.
2. `codex_bridge.py` attaches to the thread and sends it with `turn/start`,
   or `turn/steer` when a turn is already running.
3. When Codex Desktop holds the thread's writer lock, the message goes to the
   durable `codex queue` instead, which Desktop drains.
4. `setup_isolated.py` re-projects the thread; `codex_tailer.js` pushes the new
   events over the mux WebSocket so the page updates without a reload.

## Services

The desktop launcher in the Desktop folder opens Konsole, starts the 3080 web
service and its required sync daemon, and follows both journals. Closing that
terminal stops both services. Install the user-service dependencies with
`scripts/install-codex-dsh-service-dependencies.sh`; the launcher command is
maintained in scripts/start-codex-dsh-web-terminal.sh. The sync daemon does not
start by itself on login, so an inactive web UI cannot leave it running.

```
systemctl --user status codex-dsh-appserver.service   # Codex app-server (45880)
systemctl --user status codex-dsh-web.service         # DSH web UI (3080)
systemctl --user status codex-dsh-sync.service        # projection daemon
```

## Reapplying after a DSH upgrade

```
node /home/nahida/agents/sever/dsh/patch_apiproxy.js
node /home/nahida/agents/sever/dsh/patch_workspace_rows.js
systemctl --user restart codex-dsh-web.service
```

The patcher is idempotent and reports a `MISS` for any hook whose anchor moved.

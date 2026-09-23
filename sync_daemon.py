import time

import projection_writer
import setup_isolated

# Keep the rollout byte cursors in one process across sweeps. Spawning a fresh
# projector each time forced it to parse active 100+ MB rollouts from byte zero.
INTERVAL_S = 0.7

writer = projection_writer.ProjectionWriter()
try:
    while True:
        started = time.monotonic()
        setup_isolated.run()
        # Codex deltas arrive through the local socket while the sweep runs.
        # Only this main thread appends them to the projected session log.
        for _ in range(128):
            if not writer.drain():
                break
        deadline = started + INTERVAL_S
        while time.monotonic() < deadline:
            writer.drain(timeout=max(0.0, min(0.05, deadline - time.monotonic())))
finally:
    writer.close()

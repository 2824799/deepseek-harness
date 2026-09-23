#!/usr/bin/env bash
# Keep the web UI and its Codex projector in the same user-service lifecycle.
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$unit_dir/codex-dsh-web.service.d" "$unit_dir/codex-dsh-sync.service.d"
ln -sfn "$source_dir/systemd/codex-dsh-web.service.d/10-projection.conf" "$unit_dir/codex-dsh-web.service.d/10-projection.conf"
ln -sfn "$source_dir/systemd/codex-dsh-sync.service.d/10-web-lifecycle.conf" "$unit_dir/codex-dsh-sync.service.d/10-web-lifecycle.conf"

systemctl --user daemon-reload
# The web unit now owns the sync unit, so a login without the web UI should
# not leave a projector running in the background.
systemctl --user disable codex-dsh-sync.service

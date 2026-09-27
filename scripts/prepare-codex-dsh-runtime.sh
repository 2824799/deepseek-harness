#!/usr/bin/env bash
# Copy the compatible installed runtime once; all Codex patches target this copy.
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="${DSH_INSTALLED_RUNTIME_ROOT:-$HOME/.local/lib/node_modules/@deepseek-ai/dsh}"
target_dir="$repo_dir/.codex-dsh-runtime"

if [[ ! -e "$target_dir" ]]; then
  if [[ ! -f "$source_dir/lib/bin.js" ]]; then
    echo "DSH installed runtime was not found: $source_dir" >&2
    exit 1
  fi
  stage_dir=$(mktemp -d "$repo_dir/.codex-dsh-runtime.stage.XXXXXXXX")
  trap 'rm -rf -- "$stage_dir"' EXIT
  cp -a --reflink=auto "$source_dir/." "$stage_dir/"
  mv -- "$stage_dir" "$target_dir"
  trap - EXIT
fi

DSH_CODEX_RUNTIME_ROOT="$target_dir" node "$repo_dir/patch_apiproxy.js"
DSH_CODEX_RUNTIME_ROOT="$target_dir" node "$repo_dir/patch_workspace_rows.js"
DSH_CODEX_RUNTIME_ROOT="$target_dir" node "$repo_dir/scripts/patch-codex-reasoning-runtime.mjs"
echo "Codex DSH runtime ready: $target_dir"

#!/usr/bin/env bash
# The terminal owns this systemd service: closing the terminal stops the unit.
set -u

unit=codex-dsh-web.service
journal_pid=
started=false
lock_file=/home/nahida/.cache/codex-dsh-web-terminal.lock

exec 9>"$lock_file"
if ! flock -n 9; then
  echo '已有一个 DSH 后台终端在运行。按回车关闭此窗口。'
  read -r _ || true
  exit 1
fi

cleanup() {
  trap - EXIT HUP INT TERM
  if [[ -n "$journal_pid" ]]; then
    kill "$journal_pid" 2>/dev/null || true
    wait "$journal_pid" 2>/dev/null || true
  fi
  if [[ "$started" == true ]]; then
    echo '正在停止 DSH 后台...'
    systemctl --user stop "$unit"
  fi
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo '启动 DSH 网页后台（端口 3080）'
echo "关闭这个终端会停止 $unit。"
echo

# Start following before the unit so its earliest startup output is visible.
journalctl --user -u "$unit" -n 0 -f --output=cat &
journal_pid=$!

started=true
if ! systemctl --user start "$unit"; then
  echo '启动失败。按回车关闭此窗口。'
  read -r _ || true
  exit 1
fi
echo 'DSH 后台正在运行：http://127.0.0.1:3080/'

# The log follower stays attached until the terminal closes. If it exits on
# its own, the EXIT trap still stops the service we started.
wait "$journal_pid"

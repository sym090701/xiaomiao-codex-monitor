#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8080}
PID_FILE="$ROOT_DIR/.codex-usage.pid"
LOG_FILE="$ROOT_DIR/.codex-usage.log"
HOOK_SCRIPT="$ROOT_DIR/hooks/codex_display_hook.py"
HOOK_PROMPT_FILE="$ROOT_DIR/.codex-hook-prompted"

is_running() {
  [ -f "$PID_FILE" ] || return 1
  PID=$(cat "$PID_FILE")
  [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null
}

check_dependencies() {
  if ! "$PYTHON_BIN" -c "import aiohttp, requests, psutil" 2>/dev/null; then
    echo "Missing Python dependencies. Run:"
    echo "  $PYTHON_BIN -m pip install -r $ROOT_DIR/requirements.txt"
    exit 1
  fi
}

hook_is_installed() {
  "$PYTHON_BIN" "$HOOK_SCRIPT" --status >/dev/null 2>&1
}

install_hook() {
  configure_hook_endpoint
  "$PYTHON_BIN" "$HOOK_SCRIPT" --install
  : >"$HOOK_PROMPT_FILE"
}

uninstall_hook() {
  "$PYTHON_BIN" "$HOOK_SCRIPT" --uninstall
  : >"$HOOK_PROMPT_FILE"
}

configure_hook_endpoint() {
  "$PYTHON_BIN" "$HOOK_SCRIPT" --configure --host "$HOST" --port "$PORT"
}

prompt_hook_install() {
  if hook_is_installed || [ -f "$HOOK_PROMPT_FILE" ]; then
    return
  fi

  echo "可安装 Codex Hook，让 ESP32 状态条响应 PermissionRequest、UserPromptSubmit 和 Stop。"
  if [ -t 0 ] && [ -t 1 ]; then
    printf "现在安装吗？[Y/n] "
    read -r ANSWER
    case "$ANSWER" in
      n|N|no|NO|No)
        : >"$HOOK_PROMPT_FILE"
        echo "已跳过。之后可运行：$0 install-hook"
        ;;
      *)
        install_hook
        ;;
    esac
  else
    echo "当前不是交互终端，已跳过提示；可手动运行：$0 install-hook"
  fi
}

start_server() {
  if is_running; then
    echo "Server already running (PID $PID)"
    return
  fi

  rm -f "$PID_FILE"
  check_dependencies
  configure_hook_endpoint
  nohup env PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$ROOT_DIR/main.py" \
    --serve --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
  PID=$!
  echo "$PID" >"$PID_FILE"
  sleep 1

  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Server failed to start. Check $LOG_FILE"
    exit 1
  fi

  echo "Server started (PID $PID): http://$HOST:$PORT"
  echo "Log: $LOG_FILE"
}

stop_server() {
  if ! is_running; then
    rm -f "$PID_FILE"
    echo "Server is not running"
    return
  fi

  kill "$PID"
  COUNT=0
  while kill -0 "$PID" 2>/dev/null && [ "$COUNT" -lt 20 ]; do
    sleep 0.1
    COUNT=$((COUNT + 1))
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
  fi
  rm -f "$PID_FILE"
  echo "Server stopped"
}

show_status() {
  if is_running; then
    echo "Server running (PID $PID): http://$HOST:$PORT"
    return
  fi
  echo "Server is not running"
  exit 1
}

foreground_server() {
  check_dependencies
  configure_hook_endpoint
  exec "$PYTHON_BIN" "$ROOT_DIR/main.py" --serve --host "$HOST" --port "$PORT"
}

COMMAND=${1:-start}
case "$COMMAND" in
  start)
    prompt_hook_install
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    prompt_hook_install
    stop_server
    start_server
    ;;
  status)
    show_status
    ;;
  foreground)
    prompt_hook_install
    foreground_server
    ;;
  install-hook)
    install_hook
    ;;
  uninstall-hook)
    uninstall_hook
    ;;
  hook-status)
    if hook_is_installed; then
      echo "Codex Hook 已安装"
    else
      echo "Codex Hook 未安装"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|foreground|install-hook|uninstall-hook|hook-status}"
    exit 2
    ;;
esac

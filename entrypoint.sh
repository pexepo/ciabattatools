#!/bin/sh
# Start the API and the bot, and stop the container if either one dies.
#
# Both run here because BotHost gives one container. The important part is the
# failure behaviour: if the bot crashes while the API keeps serving, the
# healthcheck stays green, the host never restarts anything, and the result looks
# like a Telegram outage -- the app loads but the bot answers nothing. So the
# first process to exit takes the container down with it.
#
# POSIX sh, not bash: the slim image has no bash.

set -eu

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

# Host-deployed volumes are usually owned by root, while the image runs as
# uid 10001 (ciabatta) -- an unowned mount makes SQLite fail with "unable to
# open database file" and the bot dies on the very first read. When this script
# runs as root it fixes the ownership of the data directory and then drops
# privileges for both children, so the runtime stays non-root either way.
# setpriv is preferred over su because slim images ship no PAM.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  chown ciabatta:ciabatta /app/data
  if command -v setpriv >/dev/null 2>&1; then
    AS_USER="setpriv --reuid=10001 --regid=10001 --init-groups"
  else
    echo "[entrypoint] setpriv not found, running as root" >&2
    AS_USER=""
  fi
else
  AS_USER=""
fi

# Forward SIGTERM to both children. Without this, `docker stop` waits out the full
# grace period and then SIGKILLs, which can interrupt a write mid-transaction.
terminate() {
  # `kill 0` signals the whole process group, which is what the children join.
  # Redirected because one of them may already be gone.
  kill 0 2>/dev/null || true
}
trap terminate TERM INT

echo "[entrypoint] starting API on ${HOST}:${PORT}"
# One worker deliberately: the tracker and the rate limiter hold per-process
# state, so a second worker would double the request budget spent against MRKT
# while each half believed it was inside the limit.
${AS_USER} python -m uvicorn src.api.app:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers 1 \
  --no-access-log \
  --proxy-headers &
api_pid=$!

echo "[entrypoint] starting bot"
${AS_USER} python -m src.bot.main &
bot_pid=$!

# `wait -n` returns as soon as the first child exits, but it is a bashism -- so
# this falls back to polling where it is missing (BusyBox sh in particular).
if ! wait -n 2>/dev/null; then
  while kill -0 "${api_pid}" 2>/dev/null && kill -0 "${bot_pid}" 2>/dev/null; do
    sleep 2
  done
fi

# Which side went first matters: "bot exited" and "API exited" have entirely
# different causes, and this log line is all an operator gets.
if ! kill -0 "${api_pid}" 2>/dev/null; then
  echo "[entrypoint] API exited -- stopping container" >&2
else
  echo "[entrypoint] bot exited -- stopping container" >&2
fi

terminate
# Non-zero so the host treats this as a crash and restarts, rather than as a clean
# shutdown it should leave stopped.
exit 1

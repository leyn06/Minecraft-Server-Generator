#!/usr/bin/env bash
# Modpack Importer worker loop.
#
# Watches /queue for request files routed to this worker (filename prefix
# = JAVA_TAG), runs the official itzg/minecraft-server setup in SETUP_ONLY
# mode to build a complete server from the requested modpack, then moves
# the result to /out/<name> and copies it into Crafty's import folder.
#
# Request env files are written by the web UI and look like:
#   NAME='cobblemon-fabric'
#   MODPACK_PLATFORM='modrinth'
#   MODRINTH_MODPACK='cobblemon-fabric'
set -u
trap 'exit 0' TERM INT

JAVA_TAG="${JAVA_TAG:-java21}"
QUEUE=/queue
SCRATCH=/data
OUT=/out
LOGS=/out-logs
CRAFTY_IMPORT=/crafty-import

mkdir -p "$OUT" "$LOGS" 2>/dev/null || true

while true; do
  req=""
  for f in "$QUEUE"/${JAVA_TAG}-*.env; do
    [ -e "$f" ] || continue
    req="$f"
    break
  done

  if [ -z "$req" ]; then
    sleep 5
    continue
  fi

  NAME=""
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1090
  . "$req"
  set +a
  rm -f "$req"
  NAME="${NAME:-modpack}"

  log="$LOGS/$NAME.log"
  echo "[$(date -Iseconds)] worker(${JAVA_TAG}) starting import: ${NAME}" > "$log"
  printf '{"state":"running","name":"%s"}\n' "$NAME" > "$OUT/status.json"

  # Start from a clean scratch dir so re-imports refresh the whole pack.
  find "$SCRATCH" -mindepth 1 -delete 2>/dev/null || true

  if SETUP_ONLY=true EULA=true /image/scripts/start >> "$log" 2>&1; then
    dest="$OUT/$NAME"
    rm -rf "$dest"
    mkdir -p "$dest"
    cp -a "$SCRATCH/." "$dest/"

    # Hand the finished server straight to Crafty's import wizard.
    if [ -d "$CRAFTY_IMPORT" ]; then
      rm -rf "$CRAFTY_IMPORT/$NAME"
      mkdir -p "$CRAFTY_IMPORT/$NAME"
      cp -a "$dest/." "$CRAFTY_IMPORT/$NAME/"
      chmod -R g+rwX "$CRAFTY_IMPORT/$NAME" 2>/dev/null || true
    fi

    printf '{"state":"done","name":"%s"}\n' "$NAME" > "$OUT/status.json"
    echo "[$(date -Iseconds)] import done -> ${dest} (+ Crafty import folder)" >> "$log"
  else
    printf '{"state":"error","name":"%s"}\n' "$NAME" > "$OUT/status.json"
    echo "[$(date -Iseconds)] import FAILED - see the log above" >> "$log"
  fi
done

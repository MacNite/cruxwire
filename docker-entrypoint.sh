#!/bin/sh
# Start as root only long enough to make /data writable by the unprivileged
# 'cruxwire' user (volumes created by older images are owned by root), then
# drop privileges for the app itself. If the container is already started as
# a non-root user (compose `user:`), just run the command.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    # Only touch files that aren't already ours, so restarts stay fast.
    find /data \( ! -user cruxwire -o ! -group cruxwire \) -exec chown cruxwire:cruxwire {} + 2>/dev/null || true
    # Optional corpus archive (BENCH_CAPTURE_ENABLED) writes outside /data.
    case "$(echo "${BENCH_CAPTURE_ENABLED:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            dir="${BENCH_CORPUS_DIR:-/shared/corpus}"
            mkdir -p "$dir" 2>/dev/null || true
            find "$dir" \( ! -user cruxwire -o ! -group cruxwire \) -exec chown cruxwire:cruxwire {} + 2>/dev/null || true
            ;;
    esac
    exec setpriv --reuid=cruxwire --regid=cruxwire --init-groups "$@"
fi

exec "$@"

#!/bin/sh
# Start Cruxwire as the unprivileged "cruxwire" user.
#
# Started as root (the default): make sure the data volume belongs to the app
# user (volumes created by older root-only images are owned by root), then drop
# privileges. Started as a non-root user (compose `user:`), just run.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
APP_UID="$(id -u cruxwire)"
APP_GID="$(id -g cruxwire)"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u "$DATA_DIR")" != "$APP_UID" ]; then
        echo "entrypoint: handing $DATA_DIR to uid $APP_UID (one-time ownership fix)"
        chown -R "$APP_UID:$APP_GID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups -- "$@"
fi
exec "$@"

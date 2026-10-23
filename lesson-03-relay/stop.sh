#!/usr/bin/env bash
# Thin wrapper: the real app lives in relay/.
exec "$(dirname "$0")/relay/stop.sh" "$@"

#!/usr/bin/env bash
# Development harness: private bus + root daemon from a `nix build` result.
#   contrib/dev.sh start   # starts dbus-daemon on /tmp/xfa-dev-bus and xfad on it
#   contrib/dev.sh stop
#   XFA_BUS_ADDRESS=unix:path=/tmp/xfa-dev-bus result/bin/xfa status
set -euo pipefail
cd "$(dirname "$0")/.."
ADDR=unix:path=/tmp/xfa-dev-bus
case "${1:-start}" in
  start)
    nix build .#xfa
    [ -S /tmp/xfa-dev-bus ] || dbus-daemon --config-file=contrib/dev-bus.conf --fork --print-pid > /tmp/xfa-dev-bus.pid
    sudo modprobe ec_sys write_support=1
    sudo result/bin/xfad --address "$ADDR" --state /tmp/xfa-dev-state.json > /tmp/xfad-dev.log 2>&1 &
    echo $! > /tmp/xfad-dev.pid
    echo "xfad started (log /tmp/xfad-dev.log); use XFA_BUS_ADDRESS=$ADDR result/bin/xfa status"
    ;;
  stop)
    sudo pkill -f 'xfad --address' || true
    [ -f /tmp/xfa-dev-bus.pid ] && kill "$(cat /tmp/xfa-dev-bus.pid)" 2>/dev/null || true
    rm -f /tmp/xfa-dev-bus /tmp/xfa-dev-bus.pid /tmp/xfad-dev.pid
    ;;
  *) echo "usage: $0 start|stop"; exit 1 ;;
esac

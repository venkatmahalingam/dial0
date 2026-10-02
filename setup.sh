#!/usr/bin/env bash
# Dial 0 one-step setup, run on the SONiC switch:
#   checks -> builds the image here -> starts the container -> installs the `dial0` command
# Re-run any time (e.g. after a SONiC upgrade). Settings: dial0.conf
exec "$(dirname "$(readlink -f "$0")")/host/dial0" ctl install "$@"

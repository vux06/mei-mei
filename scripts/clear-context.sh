#!/usr/bin/env bash
# Clear Mei Mei's active conversation context.
#   * asks the RUNNING app which session is active, then clears exactly that one (aborts if it changed meanwhile)
#   * creates a brand-new Claude session (the old one is archived, never resumed), re-applies the personality,
#     keeps permanent memory, and verifies the switch before reporting success.
exec python3 "$(dirname "$(readlink -f "$0")")/meimei_ctl.py" clear "$@"

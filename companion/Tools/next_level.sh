#!/bin/sh
# Debug builds only: step the running pet to its next level (0→1→2→3→4→0).
osascript -l JavaScript -e 'ObjC.import("Foundation"); $.NSDistributedNotificationCenter.defaultCenter.postNotificationNameObjectUserInfoDeliverImmediately("app.reigns.debug.nextLevel", $(), $(), true)' >/dev/null

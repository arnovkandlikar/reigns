#!/bin/sh
# Dev harness for FR-A3: prints what ConversationReader extracts from the live Claude window.
# Usage: sh read_conversation.sh [--full] [--delay SECONDS]
#   --delay gives you time to bring the conversation you want to read to the front.
set -e
FULL=""
DELAY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --full) FULL="--full" ;;
        --delay) DELAY="$2"; shift ;;
    esac
    shift
done
cd "$(dirname "$0")/.."
OUT="${TMPDIR:-/tmp}/reigns_read_conversation"
swiftc -O -o "$OUT" Tools/ReadConversation/main.swift \
    Reigns/AX/ConversationReader.swift Reigns/Config/AXRules.swift Reigns/App/Log.swift
if [ "$DELAY" -gt 0 ]; then
    echo "Reading in $DELAY s. Bring the Claude conversation you want to test to the front now."
    sleep "$DELAY"
fi
"$OUT" Reigns/Config/AXRules.json $FULL

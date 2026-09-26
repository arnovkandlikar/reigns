#!/bin/sh
# Build and (re)launch Reigns.app.
# Set REIGNS_SIGN_IDENTITY to your "Apple Development: ..." identity (see
# `security find-identity -v -p codesigning`) so the Accessibility permission survives rebuilds.
# Without it the build is ad-hoc signed and macOS forgets the permission after every rebuild.
set -e
cd "$(dirname "$0")"

if [ -n "$REIGNS_SIGN_IDENTITY" ]; then
    set -- CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY="$REIGNS_SIGN_IDENTITY"
fi

pkill -f "Reigns.app/Contents/MacOS/Reigns" || true
xcodebuild -project Reigns.xcodeproj -target Reigns -configuration Debug \
    SYMROOT="$PWD/build" "$@" build | grep -E "error|warning: .*\.swift|BUILD"
open build/Debug/Reigns.app

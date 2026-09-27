#!/bin/sh
# Builds the files the reigns-work npm package ships (free distribution, no Apple Developer account):
#   dist/npm/payload/Reigns.zip       Release Reigns.app, Apple Silicon + Intel, ad-hoc signed
#   dist/npm/payload/engine.tar.gz    the engine source (engine/app, pyproject, shared/) from git HEAD
# Then: cd companion/dist/npm && npm publish
# Only committed files are packaged (git archive), so .env, databases and venvs never leave the Mac.
set -e
cd "$(dirname "$0")"
DIST="$PWD"
REPO="$(git rev-parse --show-toplevel)"
PAYLOAD="$DIST/npm/payload"
BUILD="$DIST/build"
rm -rf "$PAYLOAD" "$BUILD"
mkdir -p "$PAYLOAD"

echo "==> Building Reigns.app (Release, arm64 + x86_64)"
xcodebuild -project "$REPO/companion/Reigns.xcodeproj" -target Reigns -configuration Release \
    SYMROOT="$BUILD" ARCHS="arm64 x86_64" ONLY_ACTIVE_ARCH=NO \
    CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY="-" build | grep -E "error:|BUILD"
APP="$BUILD/Release/Reigns.app"

echo "==> Ad-hoc signing"
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"
lipo -archs "$APP/Contents/MacOS/Reigns"

echo "==> Zipping app"
ditto -c -k --keepParent "$APP" "$PAYLOAD/Reigns.zip"

echo "==> Packaging engine source from git HEAD"
git -C "$REPO" archive --format=tar.gz -o "$PAYLOAD/engine.tar.gz" HEAD \
    engine/app engine/pyproject.toml shared .env.example

# Safety: nothing secret may ship.
if tar -tzf "$PAYLOAD/engine.tar.gz" | grep -E '(^|/)\.env$|\.db$|\.venv/'; then
    echo "Refusing to ship secrets/databases in engine.tar.gz" >&2
    exit 1
fi

ls -lh "$PAYLOAD"
echo "==> Done. Next: cd $DIST/npm && npm publish"

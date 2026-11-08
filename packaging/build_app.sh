#!/bin/bash
# Build EPS16plusSmasher.app and EPS16plusSmasher-<version>-macOS.dmg
#
# One-time setup, using python.org Python 3.12:
#   /Library/Frameworks/Python.framework/Versions/3.12/bin/pip3 install \
#       pyinstaller mido python-rtmidi
# Then, from anywhere:
#   bash packaging/build_app.sh
set -e
cd "$(dirname "$0")/.."            # repo root
PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
VERSION=$(grep -m1 '^APP_VERSION' EPS16plusSmasher.py | cut -d'"' -f2)
APP="dist/EPS16plusSmasher.app"
DMG="dist/EPS16plusSmasher-${VERSION}-macOS.dmg"

[ -x "$PY" ] || { echo "Python 3.12 not found at $PY"; exit 1; }
for MOD in PyInstaller mido rtmidi tkinter; do
    "$PY" -c "import $MOD" 2>/dev/null || {
        echo "Missing: $MOD   (install: $PY -m pip install pyinstaller mido python-rtmidi)"
        exit 1; }
done

echo "==> Building EPS16plusSmasher $VERSION"
rm -rf build dist
"$PY" -m PyInstaller --clean --noconfirm --distpath dist --workpath build \
    packaging/EPS16plusSmasher.spec

[ -x "$APP/Contents/MacOS/EPS16plusSmasher" ] || { echo "FAIL: app binary missing"; exit 1; }
find "$APP" -name "_rtmidi*.so" | grep -q . || { echo "FAIL: rtmidi extension not bundled"; exit 1; }
find "$APP" -name "libtk*.dylib" | grep -q . || { echo "FAIL: Tk library not bundled"; exit 1; }

# Portability check: every compiled library in the bundle must load only
# from inside the bundle or from macOS itself. A reference to an outside
# path (python.org framework, Homebrew, /usr/local) works on this Mac but
# fails on a Mac without that software installed.
echo "==> Checking library references (clean-Mac portability)"
LEAKS=0
while IFS= read -r -d '' f; do
    file "$f" | grep -q "Mach-O" || continue
    BAD=$(otool -L "$f" | tail -n +2 | awk '{print $1}' \
          | grep -E '^/(Library/Frameworks|usr/local|opt/homebrew|Users)/' || true)
    if [ -n "$BAD" ]; then
        echo "  OUTSIDE REFERENCE in ${f#$APP/}:"; echo "$BAD" | sed 's/^/      /'
        LEAKS=$((LEAKS+1))
    fi
done < <(find "$APP" -type f \( -perm -u+x -o -name "*.so" -o -name "*.dylib" \) -print0)
if [ "$LEAKS" -ne 0 ]; then
    echo "FAIL: $LEAKS file(s) depend on software outside the app"; exit 1
fi
echo "  OK: no outside library references"

ARCH=$(lipo -archs "$APP/Contents/MacOS/EPS16plusSmasher")
MINOS=$(otool -l "$APP/Contents/MacOS/EPS16plusSmasher" \
        | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')
[ -n "$MINOS" ] || MINOS=$(otool -l "$APP/Contents/MacOS/EPS16plusSmasher" \
        | awk '/LC_VERSION_MIN_MACOSX/{f=1} f&&/version/{print $2; exit}')
echo "  architecture: $ARCH    minimum macOS (launcher): ${MINOS:-unknown}"

echo "==> Building disk image"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "EPS16plusSmasher $VERSION" -srcfolder "$STAGE" \
    -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "Built:"
du -sh "$APP" "$DMG"
echo
echo "Next: open \"$APP\" and run a short upload/download/Verify on the EPS."

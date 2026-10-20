#!/usr/bin/env bash
# Build dist/AutonomiNotary (single-file, no Python needed on the target).
# Build on the OLDEST Ubuntu you want to support: the binary needs a glibc at
# least as new as the build host's.
#
#   ./build_linux.sh                     # normal build
#   ANT_BUNDLE=~/.local/bin/ant ./build_linux.sh   # also embed the ant binary
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
"$PY" -m pip install --user --upgrade -r requirements-build.txt

# Qt >= 6.5 needs libxcb-cursor.so.0 for its X11 (xcb) plugin. If the build host
# has it, PyInstaller embeds it automatically. If not, fetch the Ubuntu package
# (no sudo needed) and embed the library so target machines don't need it either.
if ! ldconfig -p | grep -q 'libxcb-cursor\.so\.0'; then
    echo ">> libxcb-cursor0 not installed; downloading it to embed in the binary"
    rm -rf build/debs build/extra-libs
    mkdir -p build/debs build/extra-libs
    (cd build/debs && apt-get download libxcb-cursor0)
    dpkg-deb -x build/debs/libxcb-cursor0_*.deb build/debs/root
    cp -a build/debs/root/usr/lib/*/libxcb-cursor.so.0* build/extra-libs/
    export EXTRA_LIBS_DIR="$PWD/build/extra-libs"
fi

"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller build.spec

echo
echo ">> Built: $(pwd)/dist/AutonomiNotary"
ls -lh dist/AutonomiNotary

# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build an AppImage of corMani for this machine.
#
# NOT A RELEASE. PLAN.txt says the packages stage 8 produces are for this
# machine, and nothing is handed to another person until stage 9 has passed.
# This script exists so that "is there an AppImage" is answered by a file on
# disk rather than by a plan item, and so the same build can be repeated.
#
# It uses python-appimage when that is installed, and otherwise prints what
# to install. Bundling PySide6 and QtWebEngine is exactly what the .deb does
# NOT do — the AppImage is the portable shape; the .deb is the Debian one.
#
# © Manish Jagdish Thatte
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-dist/cormani-0.1.0-x86_64.AppImage}"
mkdir -p "$(dirname "$OUT")"

if ! command -v python-appimage >/dev/null 2>&1; then
    echo "python-appimage is not on PATH." >&2
    echo "Install it (pipx install python-appimage), then re-run:" >&2
    echo "  $0 $OUT" >&2
    echo >&2
    echo "The .deb does not need this: dpkg-buildpackage -us -uc -b" >&2
    exit 1
fi

# A recipe directory python-appimage understands: entry point + desktop + icon.
RECIPE=$(mktemp -d)
trap 'rm -rf "$RECIPE"' EXIT
mkdir -p "$RECIPE"
cat >"$RECIPE/cormani.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=corMani
Comment=Correspondence client
Exec=cormani
Icon=cormani
Categories=Network;Email;Office;
Terminal=false
EOF
cp data/cormani.svg "$RECIPE/cormani.svg"
# The installed package name; python-appimage fetches a Python and installs it.
python-appimage build app "$RECIPE" \
    --python-version 3.13 \
    --name cormani \
    --entrypoint cormani.__main__:main \
    || {
        echo "python-appimage failed; the .deb path remains:" >&2
        echo "  dpkg-buildpackage -us -uc -b" >&2
        exit 1
    }

echo "AppImage build finished under $RECIPE / dist — move the result to $OUT"

#!/usr/bin/env bash
# Launches The Witcher 3 Mod Manager inside the Witcher 3 Proton prefix.
# Usage: witcher3modmanager [--setup]   (--setup reruns the prefix setup)

set -euo pipefail

APPID=292030
NAME=witcher3modmanager
APP_DIR="/opt/$NAME"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$NAME"

notify() {
    echo "[$NAME] $1"
    command -v notify-send &> /dev/null && notify-send -a "Witcher 3 Mod Manager" -i "$NAME" "Witcher 3 Mod Manager" "$1" || true
}

mkdir -p "$DATA_DIR"

# Bump when the setup below changes so existing prefixes get it too
SETUP_VERSION=2
SETUP_MARKER="$DATA_DIR/.prefix-ready"

if [ "${1:-}" = "--setup" ] || [ "$(cat "$SETUP_MARKER" 2>/dev/null)" != "$SETUP_VERSION" ]; then
    notify "Setting up the Witcher 3 prefix, this takes a few minutes..."
    protontricks "$APPID" -q dotnetdesktop8
    # Script Merger frees its list font unless GDI+ returns a family really named
    # "Segoe UI", so install the renamed Liberation Sans and drop any substitute
    protontricks -c "
        cp '$APP_DIR'/fonts/segoeui*.ttf \"\$WINEPREFIX/drive_c/windows/Fonts/\"
        for font in 'Segoe UI:segoeui' 'Segoe UI Bold:segoeuib' 'Segoe UI Italic:segoeuii' 'Segoe UI Bold Italic:segoeuiz'; do
            wine reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts' /v \"\${font%%:*} (TrueType)\" /t REG_SZ /d \"\${font##*:}.ttf\" /f
        done
        wine reg delete 'HKCU\\Software\\Wine\\Fonts\\Replacements' /v 'Segoe UI' /f || true
        wine reg add 'HKCU\\Software\\Wine\\AppDefaults\\WitcherScriptMerger.exe\\DllOverrides' /v gdiplus /t REG_SZ /d builtin /f
    " "$APPID"
    echo "$SETUP_VERSION" > "$SETUP_MARKER"
    notify "Prefix setup complete."
fi

# Translations are loaded relative to the working directory
cd "$APP_DIR"
exec env STEAM_RUNTIME=1 protontricks-launch --appid "$APPID" "$APP_DIR/TheWitcher3ModManager.exe"

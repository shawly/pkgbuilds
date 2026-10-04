#!/usr/bin/env bash
# Launches BG3ModManager inside the Baldur's Gate 3 Proton prefix.
# Usage: bg3modmanager [--setup]   (--setup reruns the prefix setup)

set -euo pipefail

APPID=1086940
NAME=bg3modmanager
APP_DIR="/opt/$NAME"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$NAME"
EXE=BG3ModManager.exe

notify() {
    echo "[$NAME] $1"
    command -v notify-send &> /dev/null && notify-send -a "BG3 Mod Manager" -i "$NAME" "BG3 Mod Manager" "$1" || true
}

# BG3MM writes Data/, _Logs/, Orders/ and Temp/ next to its exe, and the .NET
# host resolves a symlinked exe to /opt. A real copy of the 160 KB apphost plus
# symlinks for everything else gives it a writable base dir.
mkdir -p "$DATA_DIR"
find "$DATA_DIR" -maxdepth 1 -xtype l -delete
for src in "$APP_DIR"/*; do
    dst="$DATA_DIR/$(basename "$src")"
    [ "$(basename "$src")" = "$EXE" ] && continue
    [ -L "$dst" ] || rm -rf "$dst"
    ln -sfn "$src" "$dst"
done
cmp -s "$APP_DIR/$EXE" "$DATA_DIR/$EXE" || install -m644 "$APP_DIR/$EXE" "$DATA_DIR/$EXE"

if [ "${1:-}" = "--setup" ] || [ ! -f "$DATA_DIR/.prefix-ready" ]; then
    notify "Setting up the Baldur's Gate 3 prefix, this takes a few minutes..."
    protontricks "$APPID" -q d3dcompiler_47 vcrun2022 dotnetdesktop8 arial fontsmooth=rgb
    # WPF rendering is broken under Wine with HW acceleration enabled
    protontricks -c 'wine reg add "HKCU\Software\Microsoft\Avalon.Graphics" /v DisableHWAcceleration /t REG_DWORD /d 1 /f' "$APPID"
    touch "$DATA_DIR/.prefix-ready"
    notify "Prefix setup complete."
fi

cd "$DATA_DIR"
exec env STEAM_RUNTIME=1 protontricks-launch --appid "$APPID" "$DATA_DIR/$EXE"

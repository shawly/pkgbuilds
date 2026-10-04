#!/usr/bin/env bash
# Launches The Witcher 3 Mod Manager inside the Witcher 3 Proton prefix.

set -euo pipefail

APPID=292030
APP_DIR=/opt/witcher3modmanager

# Translations are loaded relative to the working directory
cd "$APP_DIR"
exec env STEAM_RUNTIME=1 protontricks-launch --appid "$APPID" "$APP_DIR/TheWitcher3ModManager.exe"

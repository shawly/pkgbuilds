#!/usr/bin/env bash
# Applies patches/<pkgname>/*.patch onto the matching submodule's working
# tree. Not committed anywhere: every CI job does a fresh checkout, so this
# runs once per job right after checkout, before anything reads the
# PKGBUILD.
set -euo pipefail

target="${1:-}"

apply_for() {
  local dir="$1"
  local pkg
  pkg="$(basename "$dir")"
  local patchdir="patches/$pkg"

  [ -d "$patchdir" ] || return 0

  shopt -s nullglob
  local patches=("$patchdir"/*.patch)
  shopt -u nullglob
  [ ${#patches[@]} -eq 0 ] && return 0

  for p in "${patches[@]}"; do
    echo "Applying $p to $dir"
    patch -p1 -d "$dir" --batch --forward < "$p"
  done
}

if [ -n "$target" ]; then
  apply_for "$target"
else
  find . -maxdepth 2 -name PKGBUILD -print0 | while IFS= read -r -d '' file; do
    apply_for "$(dirname "$file")"
  done
fi

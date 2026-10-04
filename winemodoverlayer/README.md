# WineModOverlayer

The command is `winemodoverlayer`.

Runs a Wine or Proton game with its mods overlaid on the install directory. The
install directory is never written to, so the game folder stays exactly as the
installer or updater left it and uninstalling a mod is a `rm -rf` of one
directory.

It needs nothing from any particular launcher. Anything that sets
`STEAM_COMPAT_DATA_PATH` or `WINEPREFIX` and lets you wrap the launch command
works, Heroic and Steam included.

## How it works

The mod directories and the install directory are stacked with overlayfs,
mounted onto the install directory's own path inside a private mount namespace
created by `bwrap`. Every path the launcher passed stays valid, so neither the
launcher nor Wine can tell the difference. Unprivileged overlayfs in a user
namespace needs kernel 5.11 or newer.

Wine needs no cooperation: it opens game files with ordinary syscalls through
the drive mapping. The namespace is torn down when the game exits, so the
launcher itself only ever sees the unmodded install.

Writes go to the upper layer. That covers mod logs, ini files the mod rewrites,
shader caches, and any save the game keeps next to its executable.

An overlayfs upper layer needs user xattrs, which fuse (mergerfs, ntfs-3g,
rclone), ntfs and exfat do not provide; the mount fails with a bare `EINVAL`
there. So the upper layer sits in the mods directory only when that filesystem
can hold one, and otherwise moves to
`${XDG_DATA_HOME:-~/.local/share}/winemodoverlayer/<prefix>/`. Either way the
wrapper says where it put it. The mods and the game themselves can sit
anywhere, including on a fuse mount.

## The mods directory

One subdirectory per mod, each mirroring the game's layout from the install
root, so a file belonging at `Game/Binaries/Win64/mod.dll` lives at
`<mod>/Game/Binaries/Win64/mod.dll`.

| Entry           | Role                                          |
| --------------- | --------------------------------------------- |
| `<AnyName>/`    | one mod, a layer                              |
| `overlay.conf`  | points at the install directory               |
| `.upper/`       | everything the game writes, when it can live here |
| `.work/`        | overlayfs scratch, leave it alone             |
| `_anything/`    | ignored, somewhere to park manual installs    |

Layers apply in C sort order and later names win on conflicts, with the install
directory itself at the bottom. Names starting with `.` or `_` are not layers,
which is what keeps `.upper` out of the stack.

`overlay.conf` is sourced as shell. It sets `TARGET` to the install directory,
absolute or relative to the prefix, and may override `UPPER` and `WORK`. See
`overlay.conf.example`.

## Heroic

Settings, Advanced, Wrapper: add `/usr/bin/winemodoverlayer` with no
arguments. Heroic sets `STEAM_COMPAT_DATA_PATH` to the wine prefix, so the mods
directory is found at `<prefix>/mods` without being told.

Put it after `ludusavi` if you use that, so ludusavi stays outside the
namespace and keeps seeing the real prefix for save backups.

A game with no `mods` directory runs unchanged, so the same entry is safe in
Heroic's global wrapper list.

## Steam

Launch options:

    winemodoverlayer %command%

The mods directory is `<compatdata>/<appid>/mods`. A Steam game lives outside
its prefix, so `TARGET` has to be an absolute path there.

## Example

Clair Obscur: Expedition 33 under Heroic, with two DLL mods and two pak mods:

    <prefix>/mods/
      ClairObscurFix/       Sandfall/Binaries/Win64
      OptiScaler/           Sandfall/Binaries/Win64
      EnhancedDescriptions/ Sandfall/Content/Paks/~mods
      NoIntro/              Sandfall/Content/Paks/~mods
      overlay.conf          TARGET="drive_c/Games/Clair Obscur Expedition 33"

Both DLL mods shipped an ASI loader, and only one proxy DLL can occupy a given
name. Installing ClairObscurFix's `dsound.dll`, which is Ultimate ASI Loader,
and renaming `OptiScaler.dll` to `OptiScaler.asi` lets the one loader pull in
both. A proxy DLL also needs `WINEDLLOVERRIDES=dsound=n,b` in the environment,
otherwise Wine prefers its own builtin and the mod never loads.

## Caveats

- **A running wineserver wins.** Start the game while a wineserver for that
  prefix is already up and the new processes attach to it, outside the
  namespace, with the unmodified view. Let the prefix go cold first.
- **Case matters, and in Windows games it usually does not.** overlayfs merges
  names byte for byte, so a mod shipping `Data/` against a game's `data/` gives
  you two sibling directories and Wine's case-insensitive lookup picks one.
  Match the game's casing when unpacking a mod.
- **An `UPPER` set by hand needs `WORK` with it**, on the same filesystem, and
  that filesystem needs xattrs. Set neither and the wrapper picks a pair that
  works.
- **Deleting a stock file through the overlay leaves a whiteout** in the upper
  layer that keeps masking that path even after an update restores it. Worth
  knowing when a mod uninstall looks like it did not take.
- **Saves kept next to the executable land in the upper layer.** Good for
  keeping the install clean, surprising when you go looking for them.
- **Anti-cheat is a non-starter**, both the namespace and the modified files.

## Removing

Delete a layer directory to remove one mod. Clear the upper layer to throw
away everything the modded game ever wrote, whiteouts included. Drop the wrapper
entry, or rename the `mods` directory, to launch unmodded.

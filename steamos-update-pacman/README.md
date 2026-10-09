# steamos-update-pacman

Makes the Software Update button in Steam's game mode settings check for and
install pacman and system flatpak updates, instead of always reporting that
the system is up to date.

## How Steam reaches it

Steam runs `/usr/bin/steamos-polkit-helpers/steamos-update`, which
`jupiter-hw-support` aliases to `holo-polkit-helpers/holo-update`. That helper
re-executes itself through pkexec, with a polkit rule that needs no password,
and runs `/usr/bin/holo-update` as root. `gamescope-session-cachyos` ships
that last file as a stub that always exits 7, "nothing to update".

Every file in that chain belongs to another package, so a pacman hook copies a
one-line shim over `/usr/bin/holo-update` after each install or upgrade of
either package. The shim runs `/usr/lib/steamos-update-pacman/steamos-update`.
The stub it replaced is kept in `/var/lib/steamos-update-pacman` and put back
when this package is removed. Until then, `pacman -Qkk gamescope-session-cachyos`
reports `/usr/bin/holo-update` as modified, and that is expected.

## What it does

- Check: `checkupdates` plus `flatpak remote-ls --updates --system`. The real
  sync database is left alone, so a check can never cause a partial upgrade.
  Steam shows the counts as the version when developer mode is on.
- Update: `pacman -Syu --noconfirm`, downloading first so progress can be read
  off the package cache, then `flatpak update --system`. Steam offers a restart
  afterwards.

Output goes to the journal:

    journalctl -t steamos-update-pacman

## Not covered

- AUR packages. Helpers such as paru refuse to run as root, and building
  unattended from the game mode UI is a bad idea anyway.
- Per-user flatpak installations.
- Anything pacman wants answered. `--noconfirm` takes the default answer, which
  for a conflict means the update fails. Run `pacman -Syu` from a desktop
  session to resolve it.

The polkit rule is Valve's and allows any local user to start the update
without a password. It only installs what the configured repositories serve.

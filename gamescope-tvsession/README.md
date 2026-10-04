# gamescope-tvsession

Logs into Steam Big Picture when the TV is attached and into the Plasma desktop
otherwise, by writing Plasma Login Manager's autologin config just before the
login manager starts.

## Decision

The mode lives in `/var/lib/gamescope-tvsession/mode` and is `auto`, `desktop`,
or `gamescope`. In `auto`, attaching the screen matched by `TV_MATCH` selects
Big Picture and anything else selects the desktop. A desk monitor that is never
unplugged carries no information, so it is not consulted.

A port reporting `connected` with an empty EDID is ignored, which keeps phantom
DisplayPort connectors from counting as screens.

A TV keeps hotplug detect asserted in standby, so a cable left plugged in reads
exactly like a TV that is switched on. Leave it connected and every boot lands
in Big Picture. Pull the cable, or pin the mode to `desktop` while it stays in.

## Use

Start menu, "Game Mode", opens a mode picker. Its right-click action switches to
Big Picture immediately. From a shell:

    gamescope-tvsession                            mode, decision and screens
    sudo gamescope-tvsession gamescope --now       go to Big Picture now
    sudo gamescope-tvsession desktop --now         go back to the desktop now
    sudo gamescope-tvsession auto                  back to display detection

`--now` restarts the login manager, which ends the running session. The sudo
rule for `%wheel` is what lets the menu entry do that without a password.

Leaving Big Picture needs nothing from this package: Steam's own "Switch to
Desktop" ends the session, and the greeter comes back with the desktop already
selected. See `PreselectedSession` below.

## Naming

Everything this package installs carries the package name, so nothing it leaves
behind is ambiguous:

| Path                                                          | Role              |
| ------------------------------------------------------------- | ----------------- |
| `/usr/bin/gamescope-tvsession`                                | the only script   |
| `/etc/gamescope-tvsession.conf`                               | settings          |
| `/etc/sudoers.d/gamescope-tvsession`                          | the sudo rule     |
| `/usr/lib/systemd/system/plasmalogin.service.d/50-gamescope-tvsession.conf` | the hook |
| `/usr/share/applications/gamescope-tvsession.desktop`         | menu entry        |
| `/var/lib/gamescope-tvsession/mode`                           | state             |
| `/etc/plasmalogin.conf.d/zzz-gamescope-tvsession.conf`        | generated         |

## How it sits on Plasma Login Manager

- `gamescope-tvsession apply` runs from an `ExecStartPre=` drop-in on
  `plasmalogin.service` and writes `zzz-gamescope-tvsession.conf`. The unit's own
  `udevadm settle` runs first, so connectors are already probed.
- PLM merges `/etc/plasmalogin.conf.d` in collation order, later file winning, so
  the generated fragment outranks the `zz-steamos-autologin.conf` that CachyOS
  rewrites at runtime. Nothing has to be masked or disabled.
- `/etc/plasmalogin.conf` overrides every drop-in, the reverse of SDDM's rule.
  Keep these keys out of that file, or it will win. It does not exist by default.
- `[Greeter] PreselectedSession` beats the session the greeter remembers, and the
  greeter reads it fresh every time it appears. That is why leaving Big Picture
  returns to the desktop rather than to Big Picture again. The relevant code is
  `sessionIndex` in the greeter's `GreeterState.qml`, which prefers the
  preselected session over the last logged-in one.
- `Relogin=false` means ending a session returns to the greeter instead of
  logging straight back in.

## Not covered

Which output gamescope itself prefers is separate, and set by `OUTPUT_CONNECTOR`
in `~/.config/environment.d/`. There is no way to detect whether a TV is powered
on rather than merely cabled: HDMI hotplug detect stays asserted in standby, the
audio ELD only appears once an output is already driven, and desktop AMD cards
expose no CEC adapter.

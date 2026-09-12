# gamescope-session-tvpicker

Chooses the SDDM auto-login session from which display is attached, so plugging
in the TV boots straight into Steam Big Picture and unplugging it gives back the
normal greeter and Plasma.

## Decision

The mode lives in `/etc/sddm-session-mode` and is one of `auto`, `desktop`, or
`gamescope`. In `auto`, attaching the screen matched by `TV_MATCH` selects the
Steam session and anything else selects the greeter. A desk monitor that is
never unplugged carries no information, so the picker does not look at it.

A port that reports `connected` with an empty EDID is ignored, which keeps
phantom DisplayPort connectors from counting as screens.

A TV keeps hotplug detect asserted in standby, so a cable left plugged in reads
exactly like a TV that is switched on. Leave it connected and every boot lands
in Big Picture. Pull the cable, or pin the mode to `desktop` while it stays in.

## Configuration

`/etc/gamescope-session-tvpicker.conf` holds `TV_MATCH`, the auto-login user,
and the two session names. `TV_MATCH` ships empty, so detection does nothing
until you set it:

    cat /sys/class/drm/card1-HDMI-A-1/edid | strings -n 4

## Use

The start menu entry "Session Switcher" opens a picker, and its right-click
actions set a mode directly or switch over immediately. From a shell:

    sddm-session-mode                        # what it would do right now
    sudo sddm-session-mode gamescope --now   # switch to Steam immediately
    sudo sddm-session-mode auto              # back to screen-based decision

`--now` restarts SDDM, which ends the running session. The sudo rule for
`%wheel` is what lets the menu entry do that without a password.

## How it sits on top of gamescope-session-cachyos

- `sddm-session-picker` runs from an `ExecStartPre=` drop-in on `sddm.service`
  and writes `/etc/sddm.conf.d/zzz-session-picker.conf`.
- `/usr/lib/steamos/steam-set-session` rewrites `zz-steamos-autologin.conf` at
  runtime, so that file is left alone. The generated fragment sorts after it and
  wins.
- The CachyOS fragment only ever sets `Session`. Auto-login also needs `User`,
  which only this package writes, so it decides whether auto-login happens.
- `cachyos-gamescope-autologin.service` rewrites that same file on every Plasma
  login to force the next boot back into gamescope. It is left running: the
  generated fragment outranks whatever it writes, so it changes nothing here.
- `Relogin=false` in the generated fragment means logging out of the Steam
  session lands on the greeter instead of looping straight back in.

If a CachyOS update renames the session's `.desktop` file, update
`GAMESCOPE_SESSION` in the configuration file.

## Not covered

Which output gamescope itself prefers is separate, and set by `OUTPUT_CONNECTOR`
in `~/.config/environment.d/`. There is no way to detect whether a TV is powered
on rather than merely cabled: HDMI hotplug detect stays asserted in standby, the
audio ELD only appears once an output is already driven, and desktop AMD cards
expose no CEC adapter.

## Source layout

`etc/` and `usr/` next to the PKGBUILD mirror the installed tree, so a file's
path in this directory is the path it lands at. `package()` copies them across
and sets modes, and `source=()` is empty because makepkg resolves a local source
entry by basename only and cannot take a path with directories in it.

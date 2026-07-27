# hyprpad

Turn your phone into a mouse, keyboard and trackpad for a Wayland desktop
(Hyprland/Sway). Open a page in your phone's browser, no app to install on
either side.

- Trackpad: drag to move, tap to click, two fingers (or the `✌` hold-button)
  to scroll / right-click.
- Native keyboard capture: tap `⌨ Digitar` and type with your phone's own
  keyboard.
- Modifier keys, arrow keys (hold to repeat), Esc/Tab/Enter/Backspace/PrtSc.
- Configurable shortcut buttons (`SHORTCUTS` array in `hyprpad.py`) — add
  your own combo without touching layout.
- Optional live screenshot of the desktop (`👁 Tela`, via `grim`) so you can
  see where the cursor is.
- Installable as a PWA (add to home screen) for a fullscreen, app-like feel.

## Run

```
python3 hyprpad.py
```

Open `http://<this-machine-ip>:8123` on your phone, same network.

To keep it running (auto-starts on graphical login, restarts on crash):

```
mkdir -p ~/.config/systemd/user
cp systemd/hyprpad.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hyprpad.service
```

(Edit the `ExecStart`/`WorkingDirectory` paths in the unit if you cloned
this repo somewhere other than `~/hyprpad`.)

Env vars:
- `HYPRPAD_PORT` — default `8123`.
- `HYPRPAD_PASSWORD` — if set, requires login (cookie persists after).
- `HYPRPAD_TOKEN` — alternative to password, pass as `?t=<token>`.

## Requirements

- Python 3 + [`python-evdev`](https://python-evdev.readthedocs.io/)
- `grim` (optional, only for the live screen preview — wlroots compositors)
- Your user in the `input` group + a udev rule granting access to
  `/dev/uinput`:

  ```
  # /etc/udev/rules.d/99-uinput.rules
  KERNEL=="uinput", GROUP="input", MODE="0660"
  ```

  ```
  sudo usermod -aG input $USER
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```

  Log out/in (or reboot) for the group change to apply.

## Why

Existing phone-remote tools are mostly Windows-first or need a companion app
on both ends. This is a single Python file, browser-only, built on plain
`uinput` (works under any Wayland compositor, tested on Hyprland) — no
cloud, no account, no install beyond opening a URL.

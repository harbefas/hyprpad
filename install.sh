#!/bin/bash
# hyprpad — one-command install.
#   curl -fsSL https://raw.githubusercontent.com/harbefas/hyprpad/main/install.sh | bash
# Safe to re-run: clones/updates, installs deps, sets up uinput access, and
# installs+starts the systemd user service. Skips steps already done.
set -euo pipefail

REPO_URL="https://github.com/harbefas/hyprpad.git"
INSTALL_DIR="${HYPRPAD_DIR:-$HOME/hyprpad}"

echo "==> hyprpad install"

# 1. get the code
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "==> cloning to $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 2. dependencies — pacman on Arch/Omarchy, pip --user elsewhere
if command -v pacman >/dev/null; then
    echo "==> installing dependencies (pacman)"
    sudo pacman -S --needed --noconfirm python-evdev playerctl grim
else
    echo "==> installing python-evdev (pip --user)"
    python3 -m pip install --user --quiet evdev || {
        echo "!! pip install failed — install python-evdev manually, then re-run this script"
        exit 1
    }
    echo "!! grim/playerctl not installed automatically on non-Arch distros —"
    echo "   install them with your package manager for the live screen preview"
    echo "   and media control panel (both optional, everything else still works)."
fi

# 3. uinput access: 'input' group + udev rule
NEEDS_RELOGIN=0
if ! groups "$USER" | grep -qw input; then
    echo "==> adding $USER to the 'input' group"
    sudo usermod -aG input "$USER"
    NEEDS_RELOGIN=1
fi

UDEV_RULE=/etc/udev/rules.d/99-uinput.rules
if [ ! -f "$UDEV_RULE" ]; then
    echo "==> installing udev rule for /dev/uinput"
    echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee "$UDEV_RULE" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
fi

# 4. systemd user service
echo "==> installing systemd user service"
mkdir -p "$HOME/.config/systemd/user"
sed "s#%h/hyprpad#$INSTALL_DIR#g" systemd/hyprpad.service > "$HOME/.config/systemd/user/hyprpad.service"
systemctl --user daemon-reload
systemctl --user enable --now hyprpad.service

IP=$(ip -4 route get 1 2>/dev/null | awk '{print $7; exit}')
echo
echo "==> done"
echo "    open http://${IP:-<this-machine-ip>}:8123 on your phone (same network)"
if [ "$NEEDS_RELOGIN" = 1 ]; then
    echo "    log out and back in first — the 'input' group only applies to new sessions"
fi

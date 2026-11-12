# Install Rancho on a Raspberry Pi

Run these steps on the Pi as `tim`. The checkout and service use
`/home/tim/rancho`, on a filesystem available at boot.

## 1. Prepare the Pi and Linux environment

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Set the hostname
to `rancho`, create the `tim` user, and enable SSH with your public key. Boot the
Pi and connect over SSH; the first boot can take several minutes.

Follow [Linux setup in the config repository](../config/README.md#linux-setup)
for GitHub access, sudo, SSH, locale, shell/editor configuration, Node.js/pi, and
Tailscale. That guide clones the config repository to `/home/tim/config`.

## 2. Clone Rancho

```sh
cd /home/tim
git clone git@github.com:WebBurnout/rancho.git
cd rancho
```

If the checkout already exists, use `cd /home/tim/rancho` instead.

## 3. Install Python and uv

```sh
sudo apt-get update
sudo apt-get install -y python3 curl ca-certificates
python3 --version
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/rancho-install-uv.sh
UV_INSTALL_DIR="$HOME/.local/bin" sh /tmp/rancho-install-uv.sh
sudo install -m 0755 "$HOME/.local/bin/uv" /usr/local/bin/uv
uv --version
```

Expect apt/installer output, **Python 3.11 or newer**, and a uv version. If Python
is older, stop before continuing. If uv is already on PATH, skip its installation.

## 4. Prepare configuration and dependencies

```sh
cd /home/tim/rancho
if [ ! -e .env ]; then cp .env.example .env; fi
chmod 600 .env
uv sync --locked --python /usr/bin/python3 --no-python-downloads
```

Existing `.env` contents are preserved. Edit secrets in `.env`; `.env.example` is
the tracked template. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS`
to enable the bot; IDs are comma-separated integers. Blank settings leave Telegram
disabled.
Relative state paths are resolved against the directory containing `.env`; the
default is `state.json` in the checkout.

Expect uv to create `.venv` and install `gpiod`, `pillow`, `python-dotenv`, `python-telegram-bot`,
`reolink-aio`, Taskipy, and dependencies from `uv.lock`. Taskipy is in the default
`dev` group. Run uv as `tim`.

## 5. Link and start the service

```sh
sudo ln -sfn /home/tim/rancho/deploy/rancho.service /etc/systemd/system/rancho.service
sudo systemctl daemon-reload
sudo systemctl enable --now rancho.service
sudo systemctl status rancho.service --no-pager
sudo journalctl -u rancho.service -n 20 --no-pager
```

Expect `active (running)` and journal lines like:

```text
INFO rancho.app: Rancho started (mode=disarmed; state persistence not implemented)
INFO rancho.frontends.telegram: Telegram polling started; try /ayuda in an allowed chat
```

If status is failed or restarting, inspect the journal before continuing.

The service runs as `tim`, invoking `.venv/bin/python -m rancho` with the checkout
as its working directory. It loads `.env` there. Logs go to journald using the Pi's
existing retention settings. The service is enabled for boot and systemd restarts
it after unexpected exits.

## 6. Verify the restart task

```sh
cd /home/tim/rancho
uv run --frozen --no-sync task restart
sudo systemctl is-active rancho.service
sudo journalctl -u rancho.service -n 15 --no-pager
```

Expect `active`, shutdown messages (`Rancho stopping`, `Telegram stopped`,
`Rancho stopped`), then startup messages again. Taskipy invokes
`sudo systemctl restart rancho.service`.

Send `/ayuda` in an allowed group and expect the command list. See [README.md](README.md) for camera setup, `/foto`, and update/restart
commands.

## 7. Z-Wave board (Zooz ZAC93)

The ZAC93 sits on header pins 1–10 and talks over the UART on GPIO14 (TX,
pin 8) and GPIO15 (RX, pin 10) at 115200 baud. By default the Pi 4 gives its
good UART (PL011) to Bluetooth and puts a login console on the serial port, so
both have to move.

Shut down (`sudo poweroff`), unplug power, and seat the board on pins 1–10,
oriented as Zooz's sheet shows. Boot, then:

1. In `/boot/firmware/config.txt`, add these lines at the end, under the final
   `[all]` section:

   ```text
   enable_uart=1
   dtoverlay=disable-bt
   ```

2. In `/boot/firmware/cmdline.txt`, remove `console=serial0,115200 `. The file
   must stay a single line.

3. Disable Bluetooth, which has no adapter after `disable-bt`, and reboot:

   ```sh
   sudo systemctl disable bluetooth
   sudo reboot
   ```

After the reboot:

```sh
tail -3 /boot/firmware/config.txt
cat /boot/firmware/cmdline.txt
ls -l /dev/serial0
systemctl is-active serial-getty@ttyAMA0.service
```

Expect the two new lines, no `console=serial0` in the command line,
`/dev/serial0 -> ttyAMA0`, and `inactive`.

Check that the board answers. This sends the Serial API `GET_VERSION` request
and prints the reply as hex:

```sh
stty -F /dev/serial0 115200 raw -echo
(timeout 2 cat /dev/serial0 | xxd) &
sleep 0.3
printf '\x01\x03\x00\x15\xe9' > /dev/serial0
wait
```

Expect `06` (ACK) followed by a frame starting `01 .. 01 15` that contains the
text `Z-Wave` and a version number. No output means the board isn't answering.
Check that it is seated, that `/dev/serial0` points at `ttyAMA0` (not
`ttyS0`), and that nothing else has the port open (`sudo fuser /dev/serial0`).

## 8. Z-Wave JS server

[Z-Wave JS Server](https://github.com/zwave-js/zwave-js-server) owns the
serial port and serves the Z-Wave network as a WebSocket on `127.0.0.1:3000`;
Rancho will be its client. It runs as its own unit, `deploy/zwave.service`,
with versions pinned in `zwave/package.json` and options in `zwave/config.cjs`.
It uses Debian's Node.js (20), not nvm's, so updating nvm can't break it.

Install Node.js and the server:

```sh
sudo apt install -y nodejs npm
/usr/bin/node --version
cd /home/tim/rancho/zwave
/usr/bin/npm install
mkdir -p cache
```

Expect `v20.x`, and npm creating `node_modules/` and `package-lock.json`
(commit the lockfile; later installs use `/usr/bin/npm ci`). nvm's npm comes
first on PATH, hence the full path. `cache/` holds the network state and must
exist before the service starts.

Generate the six network keys:

```sh
for k in S0_LEGACY S2_UNAUTHENTICATED S2_AUTHENTICATED S2_ACCESS_CONTROL LR_S2_AUTHENTICATED LR_S2_ACCESS_CONTROL; do
  echo "ZWAVE_$k=$(openssl rand -hex 16)"
done
```

Add the printed lines to `/home/tim/rancho/.env` (see `.env.example`), and
save a copy off the Pi, for example in a password manager. Without the keys,
every secure device has to be excluded and paired again. Never change them
once devices are paired.

Link and start the service:

```sh
sudo ln -sfn /home/tim/rancho/deploy/zwave.service /etc/systemd/system/zwave.service
sudo systemctl daemon-reload
sudo systemctl enable --now zwave.service
sudo systemctl status zwave.service --no-pager
sudo journalctl -u zwave.service -n 50 --no-pager
```

Expect `active (running)`, the controller's details (home ID, firmware,
`USA (Long Range)` region), then `driver ready - starting server` and the
server listening on `127.0.0.1:3000`. A key error names the missing `.env`
entry. `failed starting driver` means the serial port: repeat the checks in
section 7.

Check that the server answers:

```sh
cd /home/tim/rancho/zwave
timeout 5 /usr/bin/node node_modules/.bin/zwave-client --dump | head -c 600; echo
```

Expect a `version` message with the driver and server versions, then a
`result` with the network state: one node (the controller, node 1) until
sensors are paired.

## Homelab sentinel

The sentinel runs on the Ubuntu homelab and alerts the group when the Pi stops
answering ping. The homelab must be on the same tailnet with MagicDNS, so that
`ping -c 1 rancho` works there. It needs Python 3.10+ at `/usr/bin/python3` and
no extra packages. Run these on the homelab from a checkout of this repository:

```sh
sudo install -D -m 0644 sentinel/rancho_sentinel.py /opt/rancho-sentinel/rancho_sentinel.py
sudo install -m 0644 sentinel/rancho-sentinel.service /etc/systemd/system/rancho-sentinel.service
sudo install -m 0600 sentinel/rancho-sentinel.env.example /etc/rancho-sentinel.env
sudoedit /etc/rancho-sentinel.env
sysctl net.ipv4.ping_group_range
sudo systemctl daemon-reload
sudo systemctl enable --now rancho-sentinel.service
sudo journalctl -u rancho-sentinel.service -n 20 --no-pager
```

In the env file, set the Pi's bot token and the group chat ID. The sysctl
should print `0	2147483647` (the Ubuntu default). The service runs with
`NoNewPrivileges`, so `ping` relies on that range instead of file capabilities.
Expect `INFO Watching rancho every 30s` and no warnings. A `ping rancho failed:`
warning means a DNS or permission problem.

To test, stop the Pi's Tailscale for about 4 minutes (`sudo tailscale down` on
the Pi, then `sudo tailscale up`, from a LAN session). Expect a "sin conexión"
message, then "conectado de nuevo". To update, repeat the first `install` line
and `sudo systemctl restart rancho-sentinel.service`.

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

Expect uv to create `.venv` and install `python-dotenv`, `python-telegram-bot`,
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

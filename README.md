# Rancho alarm

One Python 3.11+ asyncio service for Raspberry Pi OS Lite, managed by systemd.
One checkout at `/home/tim/rancho`, one uv-managed venv, no deployment copies.
Logs go to stdout/stderr and are captured by journald; no file logging.

## Architecture

- **Core:** configuration, alarm state, event bus, and hardware interfaces. Alarm
  behavior belongs here, independent of how users interact with it.
- **Front ends:** user interfaces built on the core. Telegram is the first front
  end; a future web UI can use the same state, events, and alarm logic.

The core does not import front ends. The asyncio application wires them together
and manages startup and shutdown.

## Installation

Follow [INSTALL.md](INSTALL.md) for setup from a fresh Raspberry Pi OS Lite image
through installing and starting Rancho. General Linux configuration lives in the
[config repository's Linux setup section](../config/README.md#linux-setup).

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS` in `.env`. IDs are
comma-separated integers; everyone in an allowed group can use the bot.

Send `/ayuda` (or `/start`) in the group to list the commands. If Telegram
routes a command to another bot, use `/ayuda@YourBotUsername`.
Other chats are silently ignored. The bot uses outbound long polling and retries
network failures. Only one process may poll this bot token; any homelab monitor
using it must send messages only.

## Alarm

| Command | Button | |
|---|---|---|
| `/armar` | Armar | Arms immediately |
| `/desarmar` | Desarmar | Disarms |
| `/estado` | Estado | Mode, since when, who changed it, and any alert pause |
| `/foto` | Foto | Photos from all cameras |
| `/pausa <hours>` | | Pauses intrusion alerts (default 1, max 24); `/pausa 0` or arming resumes them |
| `/ayuda` | | Command list |

Buttons are off: messages are sent without them. To bring them back, set
`REPLY_MARKUP = KEYBOARD` in `rancho/frontends/telegram.py`. Button presses are
still handled, so buttons on older messages keep working.
Mode changes and alert pauses are posted to every allowed chat with the
person's Telegram name (“Alarma armada (Tim)”, “Alarma desarmada (Tim)”,
“Alertas en pausa hasta 15:30 (Tim)”).

While armed, a person detected by a camera sends “Persona detectada: <camera>”
with that camera's photo. Each camera alerts at most once a minute. Nothing is
sent while disarmed or while alerts are paused with `/pausa`. Arming ends the
pause. It is kept in memory only, so a restart also resumes alerts. Cooldown and the maximum pause are
constants in `rancho/core/state.py`.

The mode is saved to `state.json` next to `.env`, only when it changes (temp
file, fsync, rename). On start, Rancho restores it and posts “Rancho
reiniciado.” with the current mode. A missing file starts disarmed; an unreadable file also starts disarmed
and the restart message says so.

## Sentinel (homelab)

`sentinel/` holds a separate, stdlib-only watchdog that runs on the Ubuntu
homelab, not on the Pi. Every 30s it pings `rancho` over Tailscale. After 6
failures in a row (~3 min) it sends “⚠️ Rancho sin conexión…” to the group, and
“✅ Rancho conectado de nuevo…” with the outage duration on recovery. If
`1.1.1.1` doesn't answer either, the homelab's own uplink is down and the round
is skipped. Unsent alerts are retried on the next check. It uses the same bot
token but only calls `sendMessage`, never `getUpdates`. Host and timings are
constants at the top of `sentinel/rancho_sentinel.py`. See
[INSTALL.md](INSTALL.md#homelab-sentinel) to install it.

## Cameras

The core uses [reolink-aio](https://github.com/starkillerOG/reolink_aio) for camera
sessions, snapshots, and native TCP event subscriptions. Each camera has its own
connection and reconnect loop.

### Snapshots

Send `/foto` in an allowed chat to get fresh photos from all Reolink
cameras. Snapshots are fetched concurrently, held in memory, and sent without
captions, in the order of `cameras.yaml`. If one camera fails, the others still
send, and a message names the camera that failed.

1. Enable **Basic service** (port 9000) in each camera's network/server
   settings. The service talks to cameras only over this native Baichuan
   protocol: setup, snapshots, and push events. HTTP, HTTPS, RTSP, and ONVIF
   can stay disabled (some models, such as the E1, have no HTTP API).
2. Set the shared credentials in `.env`:

```dotenv
CAMERA_USERNAME=viewer
CAMERA_PASSWORD=your-camera-password
```

Use the same camera account on all cameras. Its permissions must allow
capability discovery, snapshots, and event subscriptions. If a view-only account
fails initialization, verify with an admin account.

3. Update dependencies and restart the service (see [Deploy an update](#deploy-an-update)).

4. Send `/foto` (or `/foto@YourBotUsername`) in the group. Expect one photo per camera, or
   an error identifying any camera that could not provide one.

Keep camera access on the trusted LAN, keep their internet access blocked, and
do not forward camera ports. Baichuan traffic is only weakly encrypted; treat
camera credentials and session tokens as secrets. Library debug output is suppressed, and our camera
adapter logs camera names, addresses, and error types rather than raw responses. Photos stay in
memory and go to Telegram over HTTPS.

### Person detection

The service subscribes to native Baichuan TCP events on port 9000. The library
maintains the connection and updates cached AI state; callbacks read that cache
and publish core `PersonDetected` events on transitions to person detected.
Detections only alert while armed (see [Alarm](#alarm)); every detection is
logged. Walk in front of each camera while watching:

```sh
uv run --frozen --no-sync task logs
```

Expect lines such as:

```text
Camera Bomba (10.10.0.10): connected; snapshots ready
Camera Bomba (10.10.0.10): native push subscription requested on port 9000
Camera Bomba (10.10.0.10): native push received
Camera Bomba (10.10.0.10): person detected
Camera Bomba (10.10.0.10): person cleared
```

A subscription request alone does not confirm firmware/account support: verify
that walking in view produces a person event on **each** camera. Generic motion
and repeated person-positive updates do not produce additional person events.
After a disconnect, detection state is unknown; a new positive event may emit
again. There is no periodic AI-state polling or siren trigger yet.

### Offline alerts

Every 15 s each camera is checked: it counts as online only if its session is
up and it still accepts a TCP connection on port 9000 (the session alone can
look alive after a silent power or cable loss). After 60 s offline, one
`Cámara sin conexión` message goes to the allowed chats; when it returns,
`Cámara reconectada` reports how long it was down. These alerts are sent
whether the alarm is armed or not, including for a camera that never connects
after a restart. The thresholds are `OFFLINE_AFTER` and `HEALTH_INTERVAL` in
`rancho/core/cameras.py`.

## Tasks

Defined with taskipy in `pyproject.toml`:

```sh
uv run task test      # offline test suite (pytest)
uv run task logs      # follow the service journal
uv run task restart   # sudo systemctl restart rancho.service
```

## Offline tests

```sh
uv run --frozen --no-sync task test
```

These tests replace the Reolink clients and Telegram sending, and exercise
snapshots, push transitions, cleanup, partial failures, the chat allow-list,
send retries, alarm modes, alert sleep, cooldown, state persistence, and the sentinel's down/up alerts with faked ping. They do not open camera connections.

## Deploy an update

```sh
cd /home/tim/rancho
git pull
uv sync --locked
uv run --frozen --no-sync task restart
uv run --frozen --no-sync task logs
```

Expect a Git update (or already-up-to-date message), uv install/audit output, and
a service restart. `--locked` rejects a lockfile out of date with `pyproject.toml`.
After changing dependencies yourself, run `uv lock` to update the lockfile first.
For source-only edits made directly on the Pi, only the restart is needed.

After changing `deploy/rancho.service`, run this before the restart:

```sh
sudo systemctl daemon-reload
```

The symlink points directly at the tracked unit, so no copying is needed. `.env`,
state, and the venv stay local and are not tracked. Never delete the checkout as
part of a deploy. GPIO permissions, relay polarity, and physical boot/crash safety
remain for the hardware implementation step.

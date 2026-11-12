# Rancho alarm

One Python 3.12+ asyncio service for Raspberry Pi OS Lite, managed by systemd.
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
network failures. Alerts (power, intrusions, cameras, mode changes) retry for up
to 15 min, so they get through a Starlink-to-Telcel failover or a Starlink reboot
after a power cut. Each attempt is cut off after 20 s (2 min for photos),
because a connection left on the dead uplink can hang with no error. Photo
uploads run in the background, so a slow upload never delays the next alert.
Every sent alert is logged (`Telegram sent power_lost`), along with each
failed attempt. Only one process may poll this bot token; any homelab monitor
using it must send messages only.

## Alarm

| Command | Button | |
|---|---|---|
| `/armar` | Armar | Arms immediately; refused while a sensor is open |
| `/desarmar` | Desarmar | Disarms |
| `/estado` | Estado | Mode, since when, who changed it, any alert pause, cameras, and sensors |
| `/foto` | Foto | Photos from all cameras |
| `/pausa <hours>` | | Pauses intrusion alerts (default 1, max 24); `/pausa 0` or arming resumes them |
| `/encender [light]`, `/apagar [light]` | | Switches one light, or all of them, until its next scheduled time (see [Lights](#lights)) |
| `/ayuda` | | Command list |

Buttons are off: messages are sent without them. To bring them back, set
`REPLY_MARKUP = KEYBOARD` in `rancho/frontends/telegram.py`. Button presses are
still handled, so buttons on older messages keep working.
Mode changes and alert pauses are posted to every allowed chat with the
person's Telegram name (“Alarma armada (Tim)”, “Alarma desarmada (Tim)”,
“Alertas en pausa hasta 15:30 (Tim)”).

While armed, a person detected by a camera sends “Persona detectada: <camera>”
with that camera's photo and sounds the siren for 5 s. Each camera sends at most
one photo a minute, and the siren sounds at most once a minute. Nothing is sent
or sounded while disarmed or while alerts are paused with `/pausa`.

From 19:00 to 07:00 (IR mode, when false positives happen), the first detection
still sends its photo, captioned that the siren is waiting. The siren only
sounds if a second person detection, on any camera, follows within 2 min; that
detection also sends a photo, even within the camera's cooldown. Disarming
silences the siren. The siren timings and night window are constants in
`rancho/core/state.py`; equal start and end hours disable the night rule. Arming ends the
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
captions, in the order of `config.yaml`. If one camera fails, the others still
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

Keep camera access on the trusted LAN and do not forward camera ports. Baichuan
traffic is only weakly encrypted; treat camera credentials and session tokens as
secrets. Library debug output is suppressed, and our camera adapter logs camera
names, addresses, and error types rather than raw responses. Photos go to
Telegram over HTTPS and are archived on the Pi (see [Photo archive](#photo-archive)).

### Person detection

The service subscribes to native Baichuan TCP events on port 9000. The library
maintains the connection and updates cached AI state; callbacks read that cache
and publish core `PersonDetected` events on transitions to person detected.
Detections only alert, and are only logged, while armed (see [Alarm](#alarm));
while disarmed nothing about them is logged. To test, arm, then walk in front
of each camera while watching:

```sh
uv run --frozen --no-sync task logs
```

Expect lines such as:

```text
Camera Bomba (10.10.0.10): connected; snapshots ready
Camera Bomba (10.10.0.10): native push subscription requested on port 9000 (person, animal)
Camera Bomba (10.10.0.10): native push received
Intrusion: person on Bomba while armed
Camera Bomba: recording a photo every 1s
Camera Bomba: recording stopped after 8 photos
```

A subscription request alone does not confirm firmware/account support: verify
that walking in view produces a person event on **each** camera. Generic motion
and repeated person-positive updates do not produce additional person events.
After a disconnect, detection state is unknown; a new positive event may emit
again. There is no periodic AI-state polling.

## Siren and power sensing

Both use the GPIO character device through `gpiod` (the service gets the
`gpio` group).

- **Siren:** MOSFET on GPIO25, active-high by default; set
  `SIREN_ACTIVE_LOW=true` in `.env` if your driver turns on with a low pin. The
  line is claimed as an output driven OFF at start, and switched OFF and
  released at stop. If the siren can't be opened, alerts still go out and
  `/estado` and the restart message say “Sirena no disponible”. This kernel keeps outputs driven after the
  process dies (`persist_gpio_outputs=Y`), so the unit's `ExecStopPost` runs
  `python -m rancho.siren_off` after every stop, including crashes. During boot
  the pin is an input with the default pull-down, so the gate needs its own
  pull-down resistor to stay off before the service runs.
- **Power:** the 120VAC relay contact on GPIO23 (external 220 Ω pull-up, 1 kΩ in
  series; contact closed = 0 = mains present). It is polled every second and a
  change counts after 3 equal reads (about 3 s). Every outage sends “Corte de
  luz desde las HH:MM” right away, then the cameras' photos as a separate album,
  including mains already off when the service starts. When mains returns: “Volvió la luz (sin
  luz N min)”. Power cuts never sound the siren, and they alert whether or not
  the alarm is armed. `/estado` shows the mains state.

## Door and window sensors

Z-Wave sensors are listed under `sensors` in `config.yaml`, by the node number
`uv run task zwave list` shows. Rancho is a client of `zwave.service` (see
[INSTALL.md](INSTALL.md#8-z-wave-js-server)) on `ws://127.0.0.1:3000`, and
reconnects every 5 s if it goes away.

```yaml
sensors:
  - name: Ventana cocina
    node: 4
```

- **Open while armed:** “Sensor abierto: <name>” and the siren for 5 s, at any
  hour and on every opening (no night confirmation or cooldown). Nothing while
  disarmed or while alerts are paused with `/pausa`.
- **Arming** is refused while a sensor is open: “No se puede armar: sensor
  abierto: <names>”. A sensor whose state isn't known yet doesn't block it.
- **Cover removed:** “Tapa del sensor quitada: <name>”, armed or not.
- **Silent:** a battery sensor wakes on an interval (4 h on the Ecolink) even
  when nothing happens. After two missed wake-ups plus 10 min, “Sensor sin señal
  desde las HH:MM: <name>…”, armed or not, and “Sensor con señal de nuevo” when
  it is heard. A listed node that isn't paired is reported at once.
- **Server down:** if `zwave.service` can't be reached for 60 s, “Z-Wave sin
  conexión…: los sensores no funcionan”, then “Z-Wave conectado de nuevo” with the
  duration. Silence isn't reported while the server is down.

`/estado` lists each sensor: open, closed, or “comprobando…” until it is known,
plus a removed cover, silence, and the battery level. Open/closed comes from the
sensor's Access Control notification (and its binary report); a change that
happens while Rancho is disconnected is reported on reconnect. Timings are
constants in `rancho/core/sensors.py`.

## Lights

Z-Wave relays listed under `lights` in `config.yaml` are switched on a schedule,
so the house looks lived in. `on_at` and `off_at` are a quoted `"HH:MM"` or
`dusk` (civil twilight, sun 6° below the horizon, at `location`; needs the
`astral` package). Each night's times shift randomly by up to `jitter` minutes
either way (default 30).

```yaml
location:
  latitude: 18.95
  longitude: -98.75

lights:
  - name: Patio
    node: 7
    on_at: dusk
    off_at: "03:00"
```

Rancho switches a light at its on and off times, and once at service start to
match the schedule. Switching it by hand in between, at the relay or with
`/encender` / `/apagar` (all lights, or `/encender patio`), is left alone until
the next scheduled time, including across `zwave.service` reconnects. A switch the relay
doesn't confirm is retried every 30 s. Nothing goes to Telegram; `/estado` lists
each light: “encendida, se apaga a las 03:12”, “apagada a mano; el horario sigue
a las 19:04”, or “sin respuesta” when the relay can't be reached.

## Starlink dish

The dish is on the roof, so its cable could be cut. Every 15 s Rancho opens a TCP
connection to the dish's management port (`192.168.100.1:9200`, reached through
the Starlink router). This is local, so satellite dropouts don't count, and
unlike ping it needs no extra privileges. If the dish stays unreachable for 60 s
while mains is present, “Antena Starlink sin conexión desde las HH:MM, con luz.
Revisa el cable de la antena.” goes out (over the SIM backup), and
“Antena Starlink conectada de nuevo” with the duration when it answers again.
The dish is not on the UPS: during a power cut, and for 5 min after mains
returns while it boots, it is not reported. A dish still down after that does
alert, which catches a cable cut during a power cut. If the power sensor is
unavailable, mains counts as present. `/estado` shows the dish state. Timings
and the address are constants in `rancho/core/starlink.py`.

### Camera settings

Camera settings (ports, detection sensitivity, push notifications) are set in
the Reolink app. reolink-aio's Baichuan port commands don't work on this
firmware: writes are ignored or reset other ports, and reads don't match what
the camera actually has open. Turn off HTTP, HTTPS, RTSP, RTMP, and ONVIF on
each camera and leave port 9000 on.

### Photo archive

Every snapshot (alerts, power cuts, `/foto`) is saved at full camera resolution
(2560×1920 on the RLC-520A, ~0.8 MB) to `photos/YYYY-MM-DD_HHMMSS.mmm-Camera.jpg`
in the checkout, all in one folder (gitignored), for identifying people later.
When the folder passes 100 GB, the oldest photos are deleted (`MAX_BYTES` in
`rancho/core/photos.py`). Telegram gets a copy downscaled to 1280 px
(~150–250 KB), the largest size Telegram keeps for ordinary photos, which saves
SIM data. Copy files off the Pi with `scp 'rancho:rancho/photos/2026-11-11_*' .`.

While armed (and not paused), a camera that detects a person is photographed
every second until it clears, the alarm is disarmed or paused, or the camera
disconnects. Every 10 s one of those photos goes to Telegram as “<camera>:
persona aún detectada (N s)”, after the first “Persona detectada” alert. At most
30 follow-ups (5 min) are sent per detection, so a stuck detection can't flood
the chat or the SIM; archiving continues. Timings are constants in
`rancho/core/recorder.py`.

### Animal and vehicle alerts

Each camera in `config.yaml` can list `alerts`: `person` (the default),
`vehicle`, `animal`. Persons follow the alarm (photo and siren while armed).
Vehicles and animals send one photo (“Animal detectado: Bomba”) only while
armed, at most every 5 min per camera and type
(`SIGHTING_COOLDOWN` in `rancho/core/state.py`), never sound the siren, and are
silenced by `/pausa`. The photo is archived at full resolution like the others.
A type the camera's firmware doesn't support is logged at connect (“animal
detection not advertised”). The cameras always detect every type they support;
the Reolink app only sets sensitivity.

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
uv run task zwave list                 # paired Z-Wave devices: status, security, battery, route, traffic
uv run task zwave watch                # print device reports as they arrive
uv run task zwave include              # pair a device; asks for its S2 PIN if it has one
uv run task zwave exclude              # unpair, or reset a device paired elsewhere
uv run task zwave remove-failed NODE   # drop a dead device
uv run task zwave rebuild-routes [NODE]  # new routes for every device, or one; after moving a repeater
```

`rebuild-routes` without a node does every device. It exits once the awake
devices are done; `zwave.service` does each sleeping sensor at its next wake-up
(every 4 h), or right away if you remove its cover. Run it again to see
progress, or add `--follow` to watch until the sensors are done. In `list`,
“route via 7 (-71 dBm)” is the last route the controller reached the device on
(“route direct” means no repeater), with how strongly the repeater heard the
next hop toward the device. `signal` is the controller's average of the last hop
only. Both update only when the controller sends to the device (at a sensor's
wake-up). The traffic counters reset when `zwave.service` restarts.

The `zwave` task talks to `zwave.service` (see [INSTALL.md](INSTALL.md#8-z-wave-js-server))
over its own connection, so Rancho can keep running.

## Offline tests

```sh
uv run --frozen --no-sync task test
```

These tests replace the Reolink clients and Telegram sending, and exercise
snapshots, push transitions, cleanup, partial failures, the chat allow-list,
send retries, alarm modes, alert sleep, cooldown, night siren confirmation, the
siren timer, power-cut debouncing, state persistence, sensor open/close, tamper,
silence and server outages with fake Z-Wave nodes, arming refused while open,
and the sentinel's down/up alerts with faked ping. They do not open camera,
Z-Wave, or GPIO connections.

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
part of a deploy.

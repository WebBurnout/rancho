# Rancho alarm

Home alarm system on a Raspberry Pi 4 (hostname `rancho`) for a rental house in rural Mexico.

## Rules for the agent
- You are running on the Pi itself. Read-only inspection commands are allowed (for example `pwd`, `ls`, `find`, `rg`, `git status`, and `git diff`). Use the read tool for file contents. Avoid exposing secrets in command output.
- Write and edit project files as requested. Do not run commands that install/update dependencies, change system configuration or permissions, start/stop/restart services, modify Git history, or access/control alarm hardware. Do not launch the alarm service or tests that could affect the running system. Give me the exact commands for those operations; I'll run them and report back.
- Edits outside this repo require an explicit request (for example, updating `/home/tim/config/README.md`).
- Keep it simple. I'm a developer; prefer code and config files over GUIs.

## Tasks (taskipy, defined in pyproject.toml)
- `uv run task test`: offline pytest suite (fakes for cameras, Telegram, and ping). The agent may run it.
- `uv run task logs`: follow the service journal (`journalctl -u rancho.service -f -n 50`). Read-only; the agent may use `journalctl` without `-f` for inspection.
- `uv run task restart`: `sudo systemctl restart rancho.service`. Only I run it.

## Why
Thieves have stolen copper pipes and the water pump. Our power can be cut from outside, and grid outages are also frequent (usually 1–2 minutes). I want alerts with camera photos on my phone, a siren, and power-cut detection.

## Approach (decided)
- Raspberry Pi OS Lite 64-bit, already set up (locale, journald volatile, noatime, swap off, boot order). No setup script needed.
- One Python asyncio service under systemd. No Home Assistant, no Docker.
- Telegram bot is the main UI. No public IP (Starlink and Telcel are both CGNAT), so the bot uses long polling: outbound only.
- A web UI is likely later. Keep the core (state, events, hardware) separate from the Telegram layer so a web UI can be added as another front end.
- Secrets in /etc/rancho.env, never in git.
- Minimize SD card writes.

## Hardware
- Raspberry Pi 4, high-endurance microSD, Ethernet, static IP 10.10.0.2.
- Power-cut sensing: a relay with a 120VAC coil, plugged into a wall outlet. Its dry contact is wired to the Pi:
  3.3V (pin 17) -> 220 ohm -> node -> 1k ohm -> GPIO23 (pin 16); node -> relay contact -> GND (pin 20).
  Contact closed (power present) = GPIO reads 0. Contact open or wire cut = reads 1 (power out).
- Siren: 12V 15W siren switched by a relay module on GPIO25 (pin 22). Input polarity (active-high or active-low) must be configurable. The siren must stay OFF during boot, at service start and stop, and if the service crashes.
- Cameras: 3x Reolink RLC-520A PoE at 10.10.0.10, 10.10.0.11, 10.10.0.12. Internet blocked at the router; they record to their own SD cards.
- Pi, router, Telcel modem, PoE switch and cameras are on a UPS. Starlink is not.

## Network
- OpenWrt router at 10.10.0.1, LAN 10.10.0.0/24.
- Uplinks: Starlink (Wi-Fi client, interface trm_wwan) primary; Telcel 4G (`wan`) backup; mwan3 failover. A send may happen mid-failover, so outbound requests need retries.

## Alarm behavior (target)
- States: disarmed, arming (exit delay), armed. Armed state persisted to a small file, written only on change. After a reboot, restore it and notify me on Telegram.
- Person detected by any camera while armed -> photo from that camera to Telegram + siren for 10 seconds, with a per-camera cooldown. Nothing is sent while disarmed or arming.
- Power cut -> Telegram alert with snapshots if it lasts longer than a configurable delay (short blips are common). Message when power returns, with the duration.
- Camera unreachable for ~1 minute while power is present -> alert.
- Telegram buttons: Arm, Disarm, Status, Photo, Siren off. Only allow-listed chat IDs can control it.
- Monitoring: my homelab pings the Pi and alerts through the same Telegram bot. The homelab only sends messages; it must never poll for updates, because Telegram allows only one getUpdates consumer per bot token (a second one causes 409 Conflict errors).

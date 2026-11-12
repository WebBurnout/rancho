"""Pair and inspect Z-Wave devices through zwave.service: uv run task zwave --help.

Opens its own short connection to the server, so Rancho can keep running.
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Callable
from datetime import datetime

import aiohttp
from zwave_js_server.client import Client
from zwave_js_server.const import InclusionStrategy, NodeStatus, SecurityClass
from zwave_js_server.exceptions import BaseZwaveJSServerError, CannotConnect, RssiErrorReceived
from zwave_js_server.model.controller import Controller
from zwave_js_server.model.node import Node

URL = "ws://127.0.0.1:3000"
# How long the controller waits for a device to start joining or leaving.
PAIRING_WINDOW = 60
# A battery device stays awake for its interview right after joining.
INTERVIEW_WAIT = 120
BATTERY_CC = 128


class Events:
    """Queue the named events of a controller or node."""

    def __init__(self, emitter: Controller | Node, *names: str) -> None:
        self.queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._unsubscribe: list[Callable[[], None]] = [
            emitter.on(name, lambda data, name=name: self.queue.put_nowait((name, data)))
            for name in names
        ]

    async def next(self, timeout: float | None) -> tuple[str, dict]:
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()


def describe(node: Node) -> str:
    if node.is_controller_node:
        return "Controller (ZAC93)"
    config = node.device_config
    if config and config.description:
        return f"{config.manufacturer} {config.label}: {config.description}"
    if node.device_class:
        return f"{node.device_class.generic.label} (not interviewed yet)"
    return "unknown device"


def security(node: Node) -> str:
    match node.highest_security_class:
        case None:
            return "security unknown"
        case SecurityClass.NONE:
            return "insecure"
        case cls:
            return cls.name


def battery(node: Node) -> str | None:
    for value in node.values.values():
        if value.command_class == BATTERY_CC and value.property_ == "level" and value.value is not None:
            return f"battery {value.value}%"
    return None


def signal(node: Node) -> int | None:
    """Average RSSI in dBm of frames the board received from the node, if measured."""
    try:
        return node.statistics.rssi
    except RssiErrorReceived:
        return None


def noise(controller: Controller) -> str | None:
    background = controller.statistics.background_rssi
    if background is None:
        return None
    channels = [background.channel_0, background.channel_1, background.channel_2]
    averages = "/".join(f"{channel.average:.0f}" for channel in channels if channel)
    return f"background noise {averages} dBm"


def summary(node: Node) -> str:
    parts = [node.status.name.lower(), security(node)]
    if level := battery(node):
        parts.append(level)
    if (rssi := signal(node)) is not None:
        parts.append(f"signal {rssi} dBm")
    if node.last_seen:
        parts.append(f"last seen {node.last_seen.astimezone():%Y-%m-%d %H:%M}")
    return f"{node.node_id:>4}  {describe(node)}\n      {', '.join(parts)}"


async def list_nodes(controller: Controller, args: argparse.Namespace) -> int:
    for node in sorted(controller.nodes.values(), key=lambda n: n.node_id):
        print(summary(node))
    if background := noise(controller):
        print(f"\n{background}")
    return 0


async def wait_for_interview(node: Node) -> None:
    if node.ready:
        print(summary(node))
        return
    print("Interviewing; keep the device awake (press its button if it has one)...")
    events = Events(node, "interview completed", "interview failed")
    try:
        name, data = await events.next(INTERVIEW_WAIT)
    except TimeoutError:
        print("Interview still running; it finishes when the device next wakes up.")
        return
    finally:
        events.close()
    if name == "interview failed":
        print(f"Interview failed: {data.get('args', {}).get('errorMessage', 'unknown error')}")
        return
    print(summary(node))


async def include(controller: Controller, args: argparse.Namespace) -> int:
    events = Events(
        controller,
        "node found", "grant security classes", "validate dsk and enter pin",
        "node added", "inclusion failed", "inclusion stopped",
    )
    try:
        strategy = InclusionStrategy.INSECURE if args.insecure else InclusionStrategy.DEFAULT
        if not await controller.async_begin_inclusion(strategy):
            print("Could not start inclusion; is another inclusion or exclusion running?")
            return 1
        print(f"Inclusion started. Put the device in pairing mode within {PAIRING_WINDOW}s.")
        # Once a device is joining, zwave-js ends it with added or failed.
        timeout: float | None = PAIRING_WINDOW
        while True:
            try:
                name, data = await events.next(timeout)
            except TimeoutError:
                if timeout == PAIRING_WINDOW:
                    await controller.async_stop_inclusion()
                    print("No device joined.")
                else:
                    print("Inclusion stopped without a device joining.")
                return 1
            match name:
                case "node found":
                    timeout = None
                    print("Device found, joining...")
                case "grant security classes":
                    timeout = None
                    grant = data["requested_grant"]
                    print("Granting:", ", ".join(cls.name for cls in grant.security_classes))
                    await controller.async_grant_security_classes(grant)
                case "validate dsk and enter pin":
                    print(f"The device's DSK is {data['dsk']} (first 5 digits hidden).")
                    pin = await asyncio.to_thread(
                        input, "Enter the first 5 digits from the device's label: "
                    )
                    await controller.async_validate_dsk_and_enter_pin(pin.strip())
                case "node added":
                    node = data["node"]
                    print(f"Node {node.node_id} added ({security(node)}).")
                    if data.get("result", {}).get("lowSecurity"):
                        print("Warning: secure bootstrapping failed; the device joined with lower security.")
                    break
                case "inclusion failed":
                    print("Inclusion failed.")
                    return 1
                case "inclusion stopped" if timeout is not None:
                    # zwave-js stops inclusion before reporting the found node.
                    timeout = 5
    finally:
        events.close()
    await wait_for_interview(node)
    return 0


async def exclude(controller: Controller, args: argparse.Namespace) -> int:
    events = Events(controller, "node removed", "exclusion failed", "exclusion stopped")
    try:
        if not await controller.async_begin_exclusion():
            print("Could not start exclusion; is another inclusion or exclusion running?")
            return 1
        print(f"Exclusion started. Put the device in pairing mode within {PAIRING_WINDOW}s.")
        timeout: float = PAIRING_WINDOW
        while True:
            try:
                name, data = await events.next(timeout)
            except TimeoutError:
                break
            match name:
                case "node removed":
                    print(f"Node {data['node'].node_id} excluded; the device is reset and can be paired again.")
                    return 0
                case "exclusion failed":
                    print("Exclusion failed.")
                    return 1
                case "exclusion stopped":
                    # zwave-js stops exclusion before reporting the removed node.
                    timeout = 3
    finally:
        events.close()
    if timeout == PAIRING_WINDOW:
        await controller.async_stop_exclusion()
        print("No device responded.")
        return 1
    # A device from another network is reset, but no node of ours is removed.
    print("A device responded but wasn't on this network; it is reset and can be paired.")
    return 0


# Statistics update on every frame sent or received; print the signal only when it changes.
_last_signal: dict[int, int] = {}


def show_event(node: Node, name: str, data: dict) -> None:
    match name:
        case "value updated" | "value notification":
            value = data["value"]
            label = " ".join(filter(None, [value.property_name, value.property_key_name]))
            change = f"{data['args'].get('prevValue')} -> {value.value}" if name == "value updated" else value.value
            text = f"{value.command_class_name} {label}: {change}"
        case "notification":
            notification = data["notification"]
            text = f"notification {getattr(notification, 'label', '')} {getattr(notification, 'event_label', '')}"
        case "statistics updated":
            if (rssi := signal(node)) is None or _last_signal.get(node.node_id) == rssi:
                return
            _last_signal[node.node_id] = rssi
            text = f"signal {rssi} dBm"
        case _:
            text = name
    print(f"{datetime.now():%H:%M:%S}  node {node.node_id}  {text}", flush=True)


async def watch(controller: Controller, args: argparse.Namespace) -> int:
    names = (
        "value updated", "value notification", "notification", "statistics updated",
        "wake up", "sleep", "dead", "alive",
    )
    for node in controller.nodes.values():
        for name in names:
            node.on(name, lambda data, node=node, name=name: show_event(node, name, data))
    print("Watching all nodes; Ctrl-C to stop.", flush=True)
    await asyncio.Future()
    return 0


async def remove_failed(controller: Controller, args: argparse.Namespace) -> int:
    node = controller.nodes.get(args.node)
    if node is None:
        print(f"No node {args.node}.")
        return 1
    if node.status != NodeStatus.DEAD:
        print(f"Node {args.node} is {node.status.name.lower()}; the controller only removes dead nodes.")
    await controller.async_remove_failed_node(node)
    print(f"Node {args.node} removed.")
    return 0


async def run(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as http:
        client = Client(URL, http)
        try:
            await client.connect()
        except CannotConnect:
            print(f"Cannot connect to {URL}; is zwave.service running?")
            return 1
        ready = asyncio.Event()
        listening = asyncio.create_task(client.listen(ready))
        try:
            waiting = asyncio.create_task(ready.wait())
            await asyncio.wait({listening, waiting}, return_when=asyncio.FIRST_COMPLETED)
            if not ready.is_set():
                waiting.cancel()
                await listening
                print("The server closed the connection.")
                return 1
            assert client.driver
            return await args.action(client.driver.controller, args)
        finally:
            await client.disconnect()
            with contextlib.suppress(asyncio.CancelledError):
                await listening


def main() -> None:
    parser = argparse.ArgumentParser(prog="task zwave", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)
    commands.add_parser("list", help="show paired devices").set_defaults(action=list_nodes)
    include_parser = commands.add_parser("include", help="pair a device (asks for its S2 PIN)")
    include_parser.add_argument("--insecure", action="store_true", help="pair without encryption")
    include_parser.set_defaults(action=include)
    commands.add_parser(
        "exclude", help="unpair a device; also resets one paired elsewhere"
    ).set_defaults(action=exclude)
    commands.add_parser("watch", help="print device reports as they arrive").set_defaults(action=watch)
    remove_parser = commands.add_parser("remove-failed", help="drop a dead device from the controller")
    remove_parser.add_argument("node", type=int)
    remove_parser.set_defaults(action=remove_failed)
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    except BaseZwaveJSServerError as exc:
        print(f"Z-Wave error: {exc}")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()

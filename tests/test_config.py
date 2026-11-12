"""Offline config tests using temporary files."""

from datetime import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rancho.core.config import CameraConfig, Devices, LightConfig, Location, SensorConfig, load_devices, load_config


class ConfigYamlTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "config.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, text: str) -> tuple[CameraConfig, ...]:
        self.path.write_text(text, encoding="utf-8")
        return load_devices(self.path)[0]

    def load_sensors(self, text: str) -> tuple[SensorConfig, ...]:
        self.path.write_text(text, encoding="utf-8")
        return load_devices(self.path)[1]

    def load_lights(self, text: str) -> Devices:
        self.path.write_text(text, encoding="utf-8")
        return load_devices(self.path)

    def test_committed_file_is_valid(self) -> None:
        devices = load_devices(Path(__file__).parent.parent / "config.yaml")
        self.assertTrue(devices.cameras)
        self.assertTrue(devices.sensors)
        self.assertTrue(devices.lights)

    def test_reads_names_and_hosts_in_order(self) -> None:
        cameras = self.load(
            "cameras:\n"
            "  - name: Bomba\n    host: 10.10.0.10\n"
            "  - name: Entrada\n    host: 10.10.0.11\n"
        )
        self.assertEqual(cameras, (
            CameraConfig("Bomba", "10.10.0.10"), CameraConfig("Entrada", "10.10.0.11"),
        ))

    def test_alerts_default_to_person(self) -> None:
        cameras = self.load(
            "cameras:\n  - name: Patio\n    host: 10.10.0.10\n"
            "  - name: Bomba\n    host: 10.10.0.11\n    alerts: [person, animal]\n"
        )
        self.assertEqual([c.alerts for c in cameras], [("person",), ("person", "animal")])

    def test_bad_alerts_are_rejected(self) -> None:
        for alerts in ("animal", "[cat]", "[person, person]"):
            with self.subTest(alerts), self.assertRaises(ValueError):
                self.load(f"cameras:\n  - name: Bomba\n    host: 10.10.0.11\n    alerts: {alerts}\n")

    def test_missing_file_means_no_devices(self) -> None:
        self.assertEqual(load_devices(self.path), Devices())

    def test_either_list_may_be_missing(self) -> None:
        self.assertEqual(self.load("sensors:\n  - name: Cocina\n    node: 4\n"), ())
        self.assertEqual(self.load_sensors("cameras:\n  - name: Patio\n    host: 10.10.0.12\n"), ())
        self.assertEqual(load_devices(self.path), Devices(cameras=(CameraConfig("Patio", "10.10.0.12"),)))

    def test_reads_sensors_in_order(self) -> None:
        sensors = self.load_sensors(
            "sensors:\n  - name: Ventana cocina\n    node: 4\n  - name: ' Sala '\n    node: 7\n"
        )
        self.assertEqual(sensors, (SensorConfig("Ventana cocina", 4), SensorConfig("Sala", 7)))

    def test_invalid_sensors_are_rejected(self) -> None:
        cases = {
            "not a list": "sensors: Cocina\n",
            "missing node": "sensors:\n  - name: Cocina\n",
            "unknown key": "sensors:\n  - name: Cocina\n    node: 4\n    kind: door\n",
            "controller node": "sensors:\n  - name: Cocina\n    node: 1\n",
            "text node": "sensors:\n  - name: Cocina\n    node: cuatro\n",
            "bool node": "sensors:\n  - name: Cocina\n    node: true\n",
            "empty name": "sensors:\n  - name: ''\n    node: 4\n",
            "duplicate name": "sensors:\n  - name: Cocina\n    node: 4\n  - name: cocina\n    node: 5\n",
            "duplicate node": "sensors:\n  - name: Cocina\n    node: 4\n  - name: Sala\n    node: 4\n",
            "unknown section": "cameras: []\nsirens: []\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.load_sensors(text)

    def test_reads_lights_and_location(self) -> None:
        devices = self.load_lights(
            "location:\n  latitude: 20.5\n  longitude: -103\n"
            "lights:\n  - name: Foco\n    node: 7\n    on_at: dusk\n    off_at: '03:00'\n"
            "  - name: Patio\n    node: 8\n    on_at: '19:30'\n    off_at: '6:05'\n    jitter: 0\n"
        )
        self.assertEqual(devices.location, Location(20.5, -103.0))
        self.assertEqual(devices.lights, (
            LightConfig("Foco", 7, "dusk", time(3, 0)), LightConfig("Patio", 8, time(19, 30), time(6, 5), jitter=0),
        ))

    def test_invalid_lights_are_rejected(self) -> None:
        light = "lights:\n  - name: Foco\n    node: 7\n"
        cases = {
            "missing off": light + "    on_at: '19:00'\n",
            "unquoted time": light + "    on_at: 19:00\n    off_at: '03:00'\n",
            "bad time": light + "    on_at: '25:00'\n    off_at: '03:00'\n",
            "same times": light + "    on_at: '03:00'\n    off_at: '03:00'\n",
            "bad jitter": light + "    on_at: '19:00'\n    off_at: '03:00'\n    jitter: -5\n",
            "unknown key": light + "    on_at: '19:00'\n    off_at: '03:00'\n    dim: 50\n",
            "dusk without location": light + "    on_at: dusk\n    off_at: '03:00'\n",
            "node is a sensor": "sensors:\n  - name: Cocina\n    node: 7\n" + light + "    on_at: '19:00'\n    off_at: '03:00'\n",
            "duplicate node": light + "    on_at: '19:00'\n    off_at: '03:00'\n  - name: Otro\n    node: 7\n"
                              "    on_at: '19:00'\n    off_at: '03:00'\n",
            "bad location": "location:\n  latitude: 120\n  longitude: 0\n",
            "partial location": "location:\n  latitude: 20\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.load_lights(text)

    def test_load_config_reads_file_next_to_env(self) -> None:
        (self.dir / ".env").write_text("", encoding="utf-8")
        self.load("cameras:\n  - name: Patio\n    host: 10.10.0.12\n")
        config = load_config(self.dir / ".env")
        self.assertEqual(config.cameras, (CameraConfig("Patio", "10.10.0.12"),))
        self.assertEqual(config.sensors, ())
        self.assertEqual(config.state_file, self.dir.resolve() / "state.json")

    def test_invalid_files_are_rejected(self) -> None:
        cases = {
            "not yaml": "cameras: [",
            "no list": "cameras: Bomba\n",
            "not a mapping": "- Bomba\n",
            "bad host": "cameras:\n  - name: Bomba\n    host: bomba.local\n",
            "empty name": "cameras:\n  - name: ' '\n    host: 10.10.0.10\n",
            "unknown key": "cameras:\n  - name: Bomba\n    host: 10.10.0.10\n    hots: x\n",
            "duplicate name": (
                "cameras:\n  - name: Bomba\n    host: 10.10.0.10\n"
                "  - name: bomba\n    host: 10.10.0.11\n"
            ),
            "duplicate host": (
                "cameras:\n  - name: Bomba\n    host: 10.10.0.10\n"
                "  - name: Patio\n    host: 10.10.0.10\n"
            ),
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.load(text)


class EnvTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hardware_defaults(self) -> None:
        self.env.write_text("", encoding="utf-8")
        config = load_config(self.env)
        self.assertFalse(config.siren_active_low)

    def test_hardware_settings(self) -> None:
        self.env.write_text("SIREN_ACTIVE_LOW=true\n", encoding="utf-8")
        self.assertTrue(load_config(self.env).siren_active_low)

    def test_bad_hardware_settings_are_rejected(self) -> None:
        self.env.write_text("SIREN_ACTIVE_LOW=maybe\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(self.env)

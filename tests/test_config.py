"""Offline config tests using temporary files."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rancho.core.config import CameraConfig, load_cameras, load_config


class CamerasYamlTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "cameras.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def load(self, text: str) -> tuple[CameraConfig, ...]:
        self.path.write_text(text, encoding="utf-8")
        return load_cameras(self.path)

    def test_committed_file_is_valid(self) -> None:
        self.assertTrue(load_cameras(Path(__file__).parent.parent / "cameras.yaml"))

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

    def test_missing_file_means_no_cameras(self) -> None:
        self.assertEqual(load_cameras(self.path), ())

    def test_load_config_reads_file_next_to_env(self) -> None:
        (self.dir / ".env").write_text("", encoding="utf-8")
        self.load("cameras:\n  - name: Patio\n    host: 10.10.0.12\n")
        config = load_config(self.dir / ".env")
        self.assertEqual(config.cameras, (CameraConfig("Patio", "10.10.0.12"),))
        self.assertEqual(config.state_file, self.dir.resolve() / "state.json")

    def test_invalid_files_are_rejected(self) -> None:
        cases = {
            "not yaml": "cameras: [",
            "no list": "cameras: Bomba\n",
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

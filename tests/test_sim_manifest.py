"""Tests for contracts_app.sim_manifest — SIM-1 foundation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contracts_app.sim_manifest import (
    SimManifest,
    compute_config_hash,
    resolve_git_commit,
)


_FIXED_NOW = "2026-05-27T18:00:00+05:30"


def _make_manifest(**overrides) -> SimManifest:
    defaults = dict(
        run_id="018f7a-test",
        kind="sim",
        source_date="2026-05-27",
        source_coll="phase1_market_snapshots",
        label="test",
        git_commit="abc1234",
        config_hash="dummy",
        env_overrides={"ENTRY_TIME_WINDOWS": ""},
        image_digest="sha256:deadbeef",
        speed=30.0,
        created_at=_FIXED_NOW,
    )
    defaults.update(overrides)
    return SimManifest(**defaults)


class TestSimManifestValidation(unittest.TestCase):
    def test_minimal_manifest_constructs(self) -> None:
        m = _make_manifest()
        self.assertEqual(m.run_id, "018f7a-test")
        self.assertEqual(m.terminal_status, "running")  # default

    def test_run_id_required(self) -> None:
        with self.assertRaises(ValueError):
            _make_manifest(run_id="")

    def test_source_date_required(self) -> None:
        with self.assertRaises(ValueError):
            _make_manifest(source_date="")

    def test_source_coll_required(self) -> None:
        with self.assertRaises(ValueError):
            _make_manifest(source_coll="")

    def test_invalid_terminal_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _make_manifest(terminal_status="pending")  # type: ignore[arg-type]

    def test_env_overrides_must_be_mapping(self) -> None:
        with self.assertRaises(TypeError):
            _make_manifest(env_overrides=["not", "a", "mapping"])  # type: ignore[arg-type]


class TestSimManifestSerialization(unittest.TestCase):
    def test_round_trip_json(self) -> None:
        original = _make_manifest()
        serialized = original.to_json()
        revived = SimManifest.from_json(serialized)
        self.assertEqual(revived.run_id, original.run_id)
        self.assertEqual(revived.config_hash, original.config_hash)
        self.assertEqual(dict(revived.env_overrides), dict(original.env_overrides))

    def test_to_dict_has_plain_dict_env_overrides(self) -> None:
        # asdict() can leave Mapping subtypes oddly typed; we want a clean
        # dict so downstream JSON consumers don't trip.
        m = _make_manifest(env_overrides={"A": "1", "B": "2"})
        d = m.to_dict()
        self.assertIsInstance(d["env_overrides"], dict)
        self.assertEqual(d["env_overrides"], {"A": "1", "B": "2"})

    def test_completed_status_fields_optional(self) -> None:
        m = _make_manifest(
            started_at="2026-05-27T18:01:00+05:30",
            completed_at="2026-05-27T18:05:00+05:30",
            terminal_status="completed",
            sentinel_id="1716821400000-0",
        )
        revived = SimManifest.from_json(m.to_json())
        self.assertEqual(revived.terminal_status, "completed")
        self.assertEqual(revived.sentinel_id, "1716821400000-0")

    def test_json_is_stable_sorted(self) -> None:
        m1 = _make_manifest(env_overrides={"B": "2", "A": "1"})
        m2 = _make_manifest(env_overrides={"A": "1", "B": "2"})
        # Sorted keys → identical JSON
        self.assertEqual(m1.to_json(), m2.to_json())


class TestSimManifestWriteTo(unittest.TestCase):
    def test_write_creates_manifest_json(self) -> None:
        m = _make_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            path = m.write_to(run_dir)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "manifest.json")
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["run_id"], m.run_id)

    def test_write_is_immutable_refuses_overwrite(self) -> None:
        m1 = _make_manifest()
        m2 = _make_manifest(label="different")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            m1.write_to(run_dir)
            with self.assertRaises(FileExistsError):
                m2.write_to(run_dir)

    def test_write_creates_intermediate_dirs(self) -> None:
        m = _make_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "deep" / "nested" / "run-1"
            m.write_to(run_dir)
            self.assertTrue((run_dir / "manifest.json").exists())


class TestComputeConfigHash(unittest.TestCase):
    def test_deterministic_for_same_inputs(self) -> None:
        a = compute_config_hash(
            env_overrides={"X": "1"},
            image_digest="sha256:aaa",
            speed=30.0,
        )
        b = compute_config_hash(
            env_overrides={"X": "1"},
            image_digest="sha256:aaa",
            speed=30.0,
        )
        self.assertEqual(a, b)

    def test_invariant_to_dict_order(self) -> None:
        a = compute_config_hash(
            env_overrides={"B": "2", "A": "1"},
            image_digest="sha256:aaa",
            speed=30.0,
        )
        b = compute_config_hash(
            env_overrides={"A": "1", "B": "2"},
            image_digest="sha256:aaa",
            speed=30.0,
        )
        self.assertEqual(a, b)

    def test_different_env_yields_different_hash(self) -> None:
        a = compute_config_hash(env_overrides={"X": "1"}, image_digest="d", speed=1.0)
        b = compute_config_hash(env_overrides={"X": "2"}, image_digest="d", speed=1.0)
        self.assertNotEqual(a, b)

    def test_different_image_yields_different_hash(self) -> None:
        a = compute_config_hash(env_overrides={}, image_digest="d1", speed=1.0)
        b = compute_config_hash(env_overrides={}, image_digest="d2", speed=1.0)
        self.assertNotEqual(a, b)

    def test_different_speed_yields_different_hash(self) -> None:
        a = compute_config_hash(env_overrides={}, image_digest="d", speed=1.0)
        b = compute_config_hash(env_overrides={}, image_digest="d", speed=2.0)
        self.assertNotEqual(a, b)

    def test_hash_format_is_hex_sha256(self) -> None:
        h = compute_config_hash(env_overrides={}, image_digest="d", speed=1.0)
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class TestResolveGitCommit(unittest.TestCase):
    def test_returns_string_never_raises(self) -> None:
        # Whether or not we're in a git repo, this should NOT raise.
        result = resolve_git_commit()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_unknown_when_git_missing(self) -> None:
        with mock.patch(
            "contracts_app.sim_manifest.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            self.assertEqual(resolve_git_commit(), "unknown")

    def test_unknown_when_git_errors(self) -> None:
        import subprocess as _sp

        with mock.patch(
            "contracts_app.sim_manifest.subprocess.run",
            side_effect=_sp.CalledProcessError(returncode=128, cmd=["git"]),
        ):
            self.assertEqual(resolve_git_commit(), "unknown")

    def test_strips_trailing_newline(self) -> None:
        completed = mock.Mock()
        completed.stdout = "abc1234567890\n"
        with mock.patch(
            "contracts_app.sim_manifest.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(resolve_git_commit(), "abc1234567890")


if __name__ == "__main__":
    unittest.main()

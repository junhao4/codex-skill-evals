"""Model-free checks for controlled profile injection and trace evidence."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib.agent_profiles import discover_profiles, inject_profiles, profile_hashes, profile_overrides
from _lib.common import HarnessError
from _lib.trace import read_rollout_metrics


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = self.root / "case"
        self.case.mkdir()
        self.source = self.root / ".codex" / "agents"
        self.source.mkdir(parents=True)
        (self.source / "reviewer.toml").write_text('name = "reviewer"\ndescription = "Review"\ndeveloper_instructions = "Read assigned skill"\n')

    def declare(self, names):
        (self.case / "agent-profiles.json").write_text(json.dumps(names))

    def test_no_implicit_profiles(self):
        self.assertEqual(discover_profiles(self.case, self.root), ())
        self.assertEqual(profile_overrides(()), [])

    def test_missing_duplicate_and_unsafe_declarations(self):
        for names in (["missing"], ["reviewer", "reviewer"], ["../reviewer"], {"profiles": []}, [True]):
            with self.subTest(names=names):
                self.declare(names)
                with self.assertRaises(HarnessError):
                    discover_profiles(self.case, self.root)

    def test_injection_snapshot_registration_and_mutation_detection(self):
        self.declare(["reviewer"])
        profiles = discover_profiles(self.case, self.root)
        run = self.root / "run"
        run.mkdir()
        workspace = run / "workspace"
        injected = inject_profiles(profiles, workspace, run)
        source_bytes = profiles[0].read_bytes()
        self.assertEqual(injected[0].read_bytes(), source_bytes)
        self.assertEqual((run / "agent-profile-snapshot" / "reviewer.toml").read_bytes(), source_bytes)
        flags = profile_overrides(injected)
        self.assertIn(f'agents.reviewer.config_file={json.dumps(str(injected[0].resolve()))}', flags)
        before = profile_hashes(injected)
        injected[0].write_text("changed")
        self.assertNotEqual(profile_hashes(injected), before)
        self.assertEqual(profiles[0].read_bytes(), source_bytes)
        injected[0].unlink()
        self.assertNotEqual(profile_hashes(injected), before)


    def test_role_metadata_supports_current_and_legacy_field_names(self):
        path = self.root / "trace.jsonl"
        for key in ("agent_role", "agent_type"):
            for nested in (False, True):
                payload = {key: "reviewer"}
                if nested:
                    payload = {"source": {"subagent": {"thread_spawn": payload}}}
                path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
                self.assertEqual(read_rollout_metrics(path)["agent_role"], "reviewer")




if __name__ == "__main__":
    unittest.main()

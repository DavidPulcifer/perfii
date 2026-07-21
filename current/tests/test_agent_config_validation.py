from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "validate_agent_config.py"
TOOL_SPEC = importlib.util.spec_from_file_location("finance_agent_config_validator", TOOL_PATH)
if TOOL_SPEC is None or TOOL_SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"Could not load {TOOL_PATH}")
validator = importlib.util.module_from_spec(TOOL_SPEC)
sys.modules[TOOL_SPEC.name] = validator
TOOL_SPEC.loader.exec_module(validator)


class AgentConfigValidationTests(TestCase):
    def _tracked_profile(self) -> dict:
        path = REPO_ROOT / "agent-config" / "customization-profile.example.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_temp_profile(
        self, root: Path, profile: dict, *, theme: dict | None = None
    ) -> tuple[Path, Path]:
        config_dir = root / "agent-config"
        config_dir.mkdir(parents=True)
        schema_source = REPO_ROOT / "agent-config" / "customization-profile.schema.json"
        schema_path = config_dir / "customization-profile.schema.json"
        schema_path.write_text(schema_source.read_text(encoding="utf-8"), encoding="utf-8")
        profile_path = config_dir / "customization-profile.test.json"
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        if theme is not None:
            (config_dir / "theme-profile.example.json").write_text(
                json.dumps(theme, indent=2), encoding="utf-8"
            )
        return profile_path, schema_path

    def test_all_tracked_profiles_manifest_and_theme_validate(self) -> None:
        for profile_path in validator.DEFAULT_PROFILES:
            with self.subTest(profile=profile_path.name):
                parsed = validator.validate_profile(profile_path)
                self.assertEqual(parsed["schema_version"], 1)

        manifest = validator.validate_manifest()
        self.assertEqual(manifest["schema_version"], 1)
        validator.validate_compatibility_alias()

    def test_standalone_validator_does_not_write_helper_bytecode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-config-read-only-test-") as raw_temp:
            tools_dir = Path(raw_temp) / "tools"
            tools_dir.mkdir()
            validator_copy = tools_dir / "validate_agent_config.py"
            shutil.copyfile(TOOL_PATH, validator_copy)
            shutil.copyfile(REPO_ROOT / "tools" / "customize_theme.py", tools_dir / "customize_theme.py")

            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run(
                [sys.executable, str(validator_copy), "--help"],
                cwd=Path(raw_temp),
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertFalse((tools_dir / "__pycache__").exists())

    def test_duplicate_ids_and_unknown_change_references_are_rejected(self) -> None:
        profile = self._tracked_profile()
        profile.pop("appearance")
        profile["workflows"].append(deepcopy(profile["workflows"][0]))
        profile["decisions"][0]["related_change_ids"].append("missing_change")

        with tempfile.TemporaryDirectory(prefix="agent-config-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(root, profile)
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path, schema_path=schema_path, repo_root=root
                )

        rendered = str(caught.exception)
        self.assertIn("duplicate identifier 'pay_yourself_first'", rendered)
        self.assertIn("unknown requested change 'missing_change'", rendered)

    def test_active_change_requires_synthetic_acceptance_coverage(self) -> None:
        profile = self._tracked_profile()
        profile.pop("appearance")
        profile["requested_changes"].append(
            {
                "change_id": "uncovered_change",
                "category": "appearance",
                "outcome": "Use a fictional alternate color palette",
                "priority": "should_have",
                "decision_status": "confirmed",
            }
        )

        with tempfile.TemporaryDirectory(prefix="agent-config-coverage-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(root, profile)
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path, schema_path=schema_path, repo_root=root
                )

        self.assertIn(
            "must be covered by at least one synthetic acceptance scenario",
            str(caught.exception),
        )

    def test_require_ready_rejects_valid_draft_profile(self) -> None:
        template_path = REPO_ROOT / "agent-config" / "customization-profile.template.json"
        profile = json.loads(template_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(prefix="agent-config-readiness-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(root, profile)
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path,
                    schema_path=schema_path,
                    repo_root=root,
                    require_ready=True,
                )

        self.assertIn(
            "must be ready_for_implementation or implemented for this check",
            str(caught.exception),
        )

    def test_ready_profile_cannot_leave_required_confirmation_unresolved(self) -> None:
        profile = self._tracked_profile()
        profile.pop("appearance")
        profile["decisions"][0]["status"] = "assumed"
        profile["open_questions"] = ["Which routing rule is correct?"]

        with tempfile.TemporaryDirectory(prefix="agent-config-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(root, profile)
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path, schema_path=schema_path, repo_root=root
                )

        rendered = str(caught.exception)
        self.assertIn("must be confirmed before requirements are ready", rendered)
        self.assertIn("must be empty before requirements are ready", rendered)

    def test_theme_reference_uses_constrained_theme_validator(self) -> None:
        profile = self._tracked_profile()
        invalid_theme = {
            "schema_version": 1,
            "name": "Unsafe incomplete theme"
        }

        with tempfile.TemporaryDirectory(prefix="agent-config-theme-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(
                root, profile, theme=invalid_theme
            )
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path, schema_path=schema_path, repo_root=root
                )

        self.assertIn("Missing top-level keys", str(caught.exception))

    def test_neutral_template_cannot_be_marked_ready_with_placeholders(self) -> None:
        template_path = REPO_ROOT / "agent-config" / "customization-profile.template.json"
        profile = json.loads(template_path.read_text(encoding="utf-8"))
        profile["requirements_status"] = "ready_for_implementation"
        profile["requested_changes"][0]["decision_status"] = "confirmed"
        profile["decisions"][0]["status"] = "confirmed"
        profile["open_questions"] = []

        with tempfile.TemporaryDirectory(prefix="agent-config-template-test-") as raw_temp:
            root = Path(raw_temp)
            profile_path, schema_path = self._write_temp_profile(root, profile)
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_profile(
                    profile_path, schema_path=schema_path, repo_root=root
                )

        rendered = str(caught.exception)
        self.assertIn("template placeholder must be replaced", rendered)
        self.assertIn("must be resolved before requirements are ready", rendered)

    def test_manifest_rejects_missing_advertised_path(self) -> None:
        manifest_path = REPO_ROOT / "agent-config" / "project-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entry_points"]["local_web"] = "current/does-not-exist.py"

        with tempfile.TemporaryDirectory(prefix="agent-manifest-test-") as raw_temp:
            test_manifest = Path(raw_temp) / "project-manifest.json"
            test_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with self.assertRaises(validator.AgentConfigError) as caught:
                validator.validate_manifest(test_manifest, repo_root=REPO_ROOT)

        self.assertIn("referenced path does not exist", str(caught.exception))

    def test_manifest_validates_branding_and_deployment_paths(self) -> None:
        manifest_path = REPO_ROOT / "agent-config" / "project-manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = (
            ("branding", "configuration"),
            ("branding", "environment_example"),
            ("deployment", "guide"),
        )

        for surface, field in cases:
            with self.subTest(surface=surface, field=field):
                manifest = deepcopy(original)
                manifest["customization_surfaces"][surface][field] = "missing/path.txt"
                with tempfile.TemporaryDirectory(prefix="agent-manifest-path-test-") as raw_temp:
                    test_manifest = Path(raw_temp) / "project-manifest.json"
                    test_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                    with self.assertRaises(validator.AgentConfigError) as caught:
                        validator.validate_manifest(test_manifest, repo_root=REPO_ROOT)

                self.assertIn("referenced path does not exist", str(caught.exception))

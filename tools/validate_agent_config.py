#!/usr/bin/env python3
"""Validate coding-agent requirements profiles and the project manifest.

The validator is deliberately dependency-free and read-only.  It implements the
small JSON Schema subset used by the tracked customization profile, adds
cross-field checks that JSON Schema cannot express clearly, verifies repository
paths advertised by the manifest, and delegates theme validation to the
constrained theme helper.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "agent-config"
DEFAULT_SCHEMA = CONFIG_DIR / "customization-profile.schema.json"
DEFAULT_MANIFEST = CONFIG_DIR / "project-manifest.json"
DEFAULT_PROFILES = (
    CONFIG_DIR / "customization-profile.template.json",
    CONFIG_DIR / "customization-profile.pay-yourself-first.example.json",
    CONFIG_DIR / "customization-profile.example.json",
)
COMPATIBILITY_ALIAS = CONFIG_DIR / "customization-profile.example.json"
NAMED_PAY_YOURSELF_FIRST_EXAMPLE = (
    CONFIG_DIR / "customization-profile.pay-yourself-first.example.json"
)


def _load_theme_tool() -> Any:
    """Load the sibling tool without relying on the caller's Python path."""
    tool_path = Path(__file__).with_name("customize_theme.py")
    spec = importlib.util.spec_from_file_location("_finance_app_customize_theme", tool_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive import guard
        raise RuntimeError(f"Could not load theme validator from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    previous_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_dont_write
    return module


_THEME_TOOL = _load_theme_tool()


@dataclass(frozen=True)
class ValidationIssue:
    location: str
    message: str

    def render(self) -> str:
        return f"{self.location}: {self.message}"


class AgentConfigError(ValueError):
    """Raised when one or more agent-facing configuration checks fail."""

    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("\n".join(issue.render() for issue in self.issues))


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentConfigError(
            [ValidationIssue(label, f"file does not exist: {path}")]
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentConfigError(
            [
                ValidationIssue(
                    label,
                    f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                )
            ]
        ) from exc
    except OSError as exc:
        raise AgentConfigError([ValidationIssue(label, str(exc))]) from exc


def _is_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _json_equal(first: Any, second: Any) -> bool:
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    return first == second


def _validate_instance(
    value: Any,
    schema: Any,
    location: str,
    issues: list[ValidationIssue],
) -> None:
    """Validate the JSON Schema subset used by customization-profile.schema.json."""
    if not isinstance(schema, dict):
        issues.append(ValidationIssue(location, "schema node must be an object"))
        return

    if "const" in schema and not _json_equal(value, schema["const"]):
        issues.append(ValidationIssue(location, f"must equal {schema['const']!r}"))
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        allowed = ", ".join(repr(item) for item in schema["enum"])
        issues.append(ValidationIssue(location, f"must be one of: {allowed}"))

    expected_type = schema.get("type")
    if expected_type is not None:
        if not isinstance(expected_type, str) or not _is_json_type(value, expected_type):
            issues.append(ValidationIssue(location, f"must be a JSON {expected_type}"))
            return

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            issues.append(ValidationIssue(location, "schema properties must be an object"))
            return
        required = schema.get("required", [])
        if not isinstance(required, list):
            issues.append(ValidationIssue(location, "schema required must be an array"))
            return
        for key in required:
            if key not in value:
                issues.append(ValidationIssue(f"{location}.{key}", "is required"))
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                issues.append(ValidationIssue(f"{location}.{key}", "is not an allowed field"))
        for key, child_value in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_instance(child_value, child_schema, f"{location}.{key}", issues)

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            issues.append(
                ValidationIssue(location, f"must contain at least {minimum_items} item(s)")
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_instance(item, item_schema, f"{location}[{index}]", issues)

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value.strip()) < minimum_length:
            issues.append(
                ValidationIssue(location, f"must contain at least {minimum_length} character(s)")
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            issues.append(ValidationIssue(location, f"must match pattern {pattern!r}"))


def _resolve_repo_reference(
    raw_path: Any,
    repo_root: Path,
    location: str,
    issues: list[ValidationIssue],
    *,
    expected_kind: str = "any",
) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        issues.append(ValidationIssue(location, "must be a non-empty repository-relative path"))
        return None
    relative = Path(raw_path)
    if relative.is_absolute():
        issues.append(ValidationIssue(location, "must be repository-relative, not absolute"))
        return None
    root = repo_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        issues.append(ValidationIssue(location, "must not escape the repository root"))
        return None
    if not candidate.exists():
        issues.append(ValidationIssue(location, f"referenced path does not exist: {raw_path}"))
        return None
    if expected_kind == "file" and not candidate.is_file():
        issues.append(ValidationIssue(location, f"must reference a file: {raw_path}"))
    if expected_kind == "directory" and not candidate.is_dir():
        issues.append(ValidationIssue(location, f"must reference a directory: {raw_path}"))
    return candidate


def _check_unique_ids(
    profile: dict[str, Any],
    collection_name: str,
    id_field: str,
    issues: list[ValidationIssue],
) -> set[str]:
    identifiers: set[str] = set()
    collection = profile.get(collection_name)
    if not isinstance(collection, list):
        return identifiers
    for index, item in enumerate(collection):
        if not isinstance(item, dict):
            continue
        identifier = item.get(id_field)
        if not isinstance(identifier, str):
            continue
        if identifier in identifiers:
            issues.append(
                ValidationIssue(
                    f"$.{collection_name}[{index}].{id_field}",
                    f"duplicate identifier {identifier!r}",
                )
            )
        identifiers.add(identifier)
    return identifiers


def _check_change_references(
    profile: dict[str, Any], change_ids: set[str], issues: list[ValidationIssue]
) -> None:
    references = (
        ("decisions", "related_change_ids"),
        ("acceptance_scenarios", "covers_change_ids"),
    )
    for collection_name, field_name in references:
        collection = profile.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item_index, item in enumerate(collection):
            if not isinstance(item, dict) or not isinstance(item.get(field_name), list):
                continue
            seen: set[str] = set()
            for ref_index, identifier in enumerate(item[field_name]):
                location = f"$.{collection_name}[{item_index}].{field_name}[{ref_index}]"
                if not isinstance(identifier, str):
                    continue
                if identifier in seen:
                    issues.append(ValidationIssue(location, f"duplicate reference {identifier!r}"))
                seen.add(identifier)
                if identifier not in change_ids:
                    issues.append(
                        ValidationIssue(location, f"unknown requested change {identifier!r}")
                    )

    covered_change_ids: set[str] = set()
    scenarios = profile.get("acceptance_scenarios")
    if isinstance(scenarios, list):
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            covered = scenario.get("covers_change_ids")
            if isinstance(covered, list):
                covered_change_ids.update(
                    identifier for identifier in covered if isinstance(identifier, str)
                )

    requested_changes = profile.get("requested_changes")
    if isinstance(requested_changes, list):
        for index, change in enumerate(requested_changes):
            if not isinstance(change, dict):
                continue
            identifier = change.get("change_id")
            if not isinstance(identifier, str):
                continue
            if (
                change.get("priority") != "deferred"
                and change.get("decision_status") != "deferred"
                and identifier not in covered_change_ids
            ):
                issues.append(
                    ValidationIssue(
                        f"$.requested_changes[{index}].change_id",
                        "must be covered by at least one synthetic acceptance scenario",
                    )
                )


def _check_readiness(profile: dict[str, Any], issues: list[ValidationIssue]) -> None:
    if profile.get("requirements_status") not in {"ready_for_implementation", "implemented"}:
        return
    for collection_name, field_name in (
        ("requested_changes", "decision_status"),
        ("workflows", "decision_status"),
    ):
        collection = profile.get(collection_name)
        if not isinstance(collection, list):
            continue
        for index, item in enumerate(collection):
            if isinstance(item, dict) and item.get(field_name) == "unresolved":
                issues.append(
                    ValidationIssue(
                        f"$.{collection_name}[{index}].{field_name}",
                        "cannot be unresolved when requirements are ready",
                    )
                )
    decisions = profile.get("decisions")
    if isinstance(decisions, list):
        for index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                continue
            if decision.get("confirmation_required") is True and decision.get("status") != "confirmed":
                issues.append(
                    ValidationIssue(
                        f"$.decisions[{index}].status",
                        "must be confirmed before requirements are ready",
                    )
                )
    open_questions = profile.get("open_questions")
    if isinstance(open_questions, list) and open_questions:
        issues.append(
            ValidationIssue(
                "$.open_questions",
                "must be empty before requirements are ready for implementation",
            )
        )

    def walk_strings(value: Any, location: str) -> Iterable[tuple[str, str]]:
        if isinstance(value, str):
            yield location, value
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from walk_strings(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk_strings(child, f"{location}[{index}]")

    for location, value in walk_strings(profile, "$"):
        normalized = value.strip().lower()
        if normalized in {"undecided", "unresolved"}:
            issues.append(
                ValidationIssue(location, "must be resolved before requirements are ready")
            )
        elif "replace this" in normalized:
            issues.append(
                ValidationIssue(location, "template placeholder must be replaced before implementation")
            )


def _check_data_posture(profile: dict[str, Any], issues: list[ValidationIssue]) -> None:
    posture = profile.get("data_posture")
    if not isinstance(posture, dict):
        return
    if (
        posture.get("starting_point") == "existing_ledger"
        and posture.get("backup_before_data_change") != "required"
    ):
        issues.append(
            ValidationIssue(
                "$.data_posture.backup_before_data_change",
                "must be required when starting from an existing ledger",
            )
        )


def _check_schema_reference(
    profile: dict[str, Any],
    profile_path: Path,
    schema_path: Path,
    issues: list[ValidationIssue],
) -> None:
    raw_reference = profile.get("$schema")
    if not isinstance(raw_reference, str) or not raw_reference.strip():
        return
    reference = Path(raw_reference)
    resolved = (
        reference.resolve()
        if reference.is_absolute()
        else (profile_path.parent / reference).resolve()
    )
    if not resolved.exists():
        issues.append(
            ValidationIssue("$.$schema", f"referenced schema does not exist: {raw_reference}")
        )
    elif resolved != schema_path.resolve():
        issues.append(
            ValidationIssue(
                "$.$schema",
                f"references {resolved}, but validation used {schema_path.resolve()}",
            )
        )


def _check_theme_reference(
    profile: dict[str, Any], repo_root: Path, issues: list[ValidationIssue]
) -> None:
    appearance = profile.get("appearance")
    if not isinstance(appearance, dict) or "theme_profile" not in appearance:
        return
    theme_path = _resolve_repo_reference(
        appearance["theme_profile"],
        repo_root,
        "$.appearance.theme_profile",
        issues,
        expected_kind="file",
    )
    if theme_path is None or not theme_path.is_file():
        return
    try:
        _THEME_TOOL.load_and_validate(theme_path)
    except (OSError, _THEME_TOOL.ThemeProfileError) as exc:
        issues.append(ValidationIssue("$.appearance.theme_profile", str(exc)))


def validate_profile(
    profile_path: Path,
    *,
    schema_path: Path = DEFAULT_SCHEMA,
    repo_root: Path = REPO_ROOT,
    require_ready: bool = False,
) -> dict[str, Any]:
    """Validate one requirements profile and return its parsed object."""
    profile_path = Path(profile_path).resolve()
    schema_path = Path(schema_path).resolve()
    profile = _read_json(profile_path, str(profile_path))
    schema = _read_json(schema_path, str(schema_path))
    issues: list[ValidationIssue] = []

    if not isinstance(profile, dict):
        issues.append(ValidationIssue("$", "profile must be a JSON object"))
    if not isinstance(schema, dict):
        issues.append(ValidationIssue("schema", "schema must be a JSON object"))
    if issues:
        raise AgentConfigError(issues)

    if schema.get("properties", {}).get("schema_version", {}).get("const") != 1:
        issues.append(ValidationIssue("schema.properties.schema_version", "must declare const 1"))
    _validate_instance(profile, schema, "$", issues)
    _check_schema_reference(profile, profile_path, schema_path, issues)

    change_ids = _check_unique_ids(profile, "requested_changes", "change_id", issues)
    _check_unique_ids(profile, "account_roles", "role_id", issues)
    _check_unique_ids(profile, "workflows", "workflow_id", issues)
    _check_unique_ids(profile, "decisions", "decision_id", issues)
    _check_unique_ids(profile, "acceptance_scenarios", "scenario_id", issues)
    _check_change_references(profile, change_ids, issues)
    _check_readiness(profile, issues)
    if require_ready and profile.get("requirements_status") not in {
        "ready_for_implementation",
        "implemented",
    }:
        issues.append(
            ValidationIssue(
                "$.requirements_status",
                "must be ready_for_implementation or implemented for this check",
            )
        )
    _check_data_posture(profile, issues)
    _check_theme_reference(profile, Path(repo_root), issues)

    if issues:
        raise AgentConfigError(issues)
    return profile


def _require_mapping(
    value: Any, location: str, issues: list[ValidationIssue]
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        issues.append(ValidationIssue(location, "must be a JSON object"))
        return None
    return value


def _require_string(
    value: Any, location: str, issues: list[ValidationIssue]
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(ValidationIssue(location, "must be a non-empty string"))
        return None
    return value


def _check_string_mapping(value: Any, location: str, issues: list[ValidationIssue]) -> None:
    mapping = _require_mapping(value, location, issues)
    if mapping is None:
        return
    for key, item in mapping.items():
        _require_string(item, f"{location}.{key}", issues)


def _check_string_list(value: Any, location: str, issues: list[ValidationIssue]) -> None:
    if not isinstance(value, list):
        issues.append(ValidationIssue(location, "must be a JSON array"))
        return
    for index, item in enumerate(value):
        _require_string(item, f"{location}[{index}]", issues)


def _check_manifest_path(
    raw_path: Any,
    repo_root: Path,
    location: str,
    issues: list[ValidationIssue],
    *,
    kind: str = "file",
) -> Path | None:
    return _resolve_repo_reference(
        raw_path, repo_root, location, issues, expected_kind=kind
    )


def validate_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Validate stable manifest fields and every advertised repository path."""
    manifest_path = Path(manifest_path).resolve()
    repo_root = Path(repo_root).resolve()
    manifest = _read_json(manifest_path, str(manifest_path))
    issues: list[ValidationIssue] = []
    if not isinstance(manifest, dict):
        raise AgentConfigError([ValidationIssue("manifest", "must be a JSON object")])

    if manifest.get("schema_version") != 1 or isinstance(manifest.get("schema_version"), bool):
        issues.append(ValidationIssue("manifest.schema_version", "must be integer 1"))
    _require_string(manifest.get("project_name"), "manifest.project_name", issues)
    source_root = _require_string(manifest.get("source_root"), "manifest.source_root", issues)
    if source_root is not None:
        _check_manifest_path(
            source_root, repo_root, "manifest.source_root", issues, kind="directory"
        )
    reviewed = _require_string(manifest.get("last_reviewed"), "manifest.last_reviewed", issues)
    if reviewed is not None:
        try:
            date.fromisoformat(reviewed)
        except ValueError:
            issues.append(ValidationIssue("manifest.last_reviewed", "must use YYYY-MM-DD"))

    _check_string_mapping(manifest.get("stack"), "manifest.stack", issues)
    _check_string_mapping(
        manifest.get("deployment_status"), "manifest.deployment_status", issues
    )
    _check_string_list(
        manifest.get("financial_invariants"), "manifest.financial_invariants", issues
    )

    entry_points = _require_mapping(manifest.get("entry_points"), "manifest.entry_points", issues)
    if entry_points is not None:
        for key, value in entry_points.items():
            reference = _require_string(value, f"manifest.entry_points.{key}", issues)
            if reference is not None:
                file_part = reference.split(":", 1)[0]
                _check_manifest_path(
                    file_part, repo_root, f"manifest.entry_points.{key}", issues
                )

    architecture = _require_mapping(
        manifest.get("architecture"), "manifest.architecture", issues
    )
    if architecture is not None:
        for key, value in architecture.items():
            _check_manifest_path(
                value,
                repo_root,
                f"manifest.architecture.{key}",
                issues,
                kind="any",
            )

    data_topology = _require_mapping(
        manifest.get("data_topology"), "manifest.data_topology", issues
    )
    if data_topology is not None:
        _require_string(data_topology.get("registry"), "manifest.data_topology.registry", issues)
        _require_string(data_topology.get("ledgers"), "manifest.data_topology.ledgers", issues)
        _check_string_list(
            data_topology.get("runtime_paths"),
            "manifest.data_topology.runtime_paths",
            issues,
        )

    surfaces = _require_mapping(
        manifest.get("customization_surfaces"),
        "manifest.customization_surfaces",
        issues,
    )
    manifest_theme_path: Path | None = None
    if surfaces is not None:
        required_path_fields = {
            "requirements_profile": {"example", "schema", "guide"},
            "appearance": {"profile", "tool", "generated_stylesheet", "guide"},
            "deployment": {"guide", "historical_private_server_reference"},
        }
        for surface_name, field_names in required_path_fields.items():
            surface = _require_mapping(
                surfaces.get(surface_name),
                f"manifest.customization_surfaces.{surface_name}",
                issues,
            )
            if surface is None:
                continue
            for field_name in field_names:
                if field_name not in surface:
                    issues.append(
                        ValidationIssue(
                            f"manifest.customization_surfaces.{surface_name}.{field_name}",
                            "is required",
                        )
                    )
            for field_name, field_value in surface.items():
                location = f"manifest.customization_surfaces.{surface_name}.{field_name}"
                if field_name == "ignored_personal_profile":
                    _require_string(field_value, location, issues)
                    continue
                resolved_path = _check_manifest_path(
                    field_value,
                    repo_root,
                    location,
                    issues,
                )
                if surface_name == "appearance" and field_name == "profile":
                    manifest_theme_path = resolved_path
        for descriptive_name in ("terminology", "workflows"):
            _require_string(
                surfaces.get(descriptive_name),
                f"manifest.customization_surfaces.{descriptive_name}",
                issues,
            )
        known_surfaces = set(required_path_fields) | {"terminology", "workflows"}
        path_field_names = {
            "builder",
            "configuration",
            "environment_example",
            "example",
            "generated_stylesheet",
            "guide",
            "historical_private_server_reference",
            "profile",
            "schema",
            "template",
            "tool",
            "third_party_notices",
            "validator",
            "worked_example",
        }
        for surface_name, surface_value in surfaces.items():
            if surface_name in known_surfaces or not isinstance(surface_value, dict):
                continue
            for field_name, field_value in surface_value.items():
                location = f"manifest.customization_surfaces.{surface_name}.{field_name}"
                if field_name == "ignored_personal_profile":
                    _require_string(field_value, location, issues)
                elif field_name in path_field_names:
                    _check_manifest_path(field_value, repo_root, location, issues)

    commands = manifest.get("verification_commands")
    if not isinstance(commands, list) or not commands:
        issues.append(
            ValidationIssue("manifest.verification_commands", "must be a non-empty array")
        )
    else:
        for index, command in enumerate(commands):
            location = f"manifest.verification_commands[{index}]"
            mapping = _require_mapping(command, location, issues)
            if mapping is None:
                continue
            _require_string(mapping.get("purpose"), f"{location}.purpose", issues)
            working_directory = _require_string(
                mapping.get("working_directory"), f"{location}.working_directory", issues
            )
            if working_directory is not None:
                _check_manifest_path(
                    working_directory,
                    repo_root,
                    f"{location}.working_directory",
                    issues,
                    kind="directory",
                )
            _require_string(mapping.get("command"), f"{location}.command", issues)
            if "condition" in mapping:
                _require_string(mapping.get("condition"), f"{location}.condition", issues)
            writes = mapping.get("writes")
            if not isinstance(writes, (bool, str)):
                issues.append(
                    ValidationIssue(f"{location}.writes", "must be a boolean or explanatory string")
                )

    runtime_ai = _require_mapping(manifest.get("runtime_ai"), "manifest.runtime_ai", issues)
    if runtime_ai is not None:
        for key in ("openai_api_required", "chat_integration_required"):
            if not isinstance(runtime_ai.get(key), bool):
                issues.append(ValidationIssue(f"manifest.runtime_ai.{key}", "must be boolean"))

    evidence = _require_mapping(
        manifest.get("verification_evidence"), "manifest.verification_evidence", issues
    )
    if evidence is not None:
        _require_string(
            evidence.get("environment"), "manifest.verification_evidence.environment", issues
        )
        verified_on = _require_string(
            evidence.get("verified_on"), "manifest.verification_evidence.verified_on", issues
        )
        if verified_on is not None:
            try:
                date.fromisoformat(verified_on)
            except ValueError:
                issues.append(
                    ValidationIssue(
                        "manifest.verification_evidence.verified_on", "must use YYYY-MM-DD"
                    )
                )
        _check_string_list(
            evidence.get("passed"), "manifest.verification_evidence.passed", issues
        )
        _require_string(
            evidence.get("scope_limit"),
            "manifest.verification_evidence.scope_limit",
            issues,
        )

    safety_docs = manifest.get("safety_docs")
    _check_string_list(safety_docs, "manifest.safety_docs", issues)
    if isinstance(safety_docs, list):
        for index, reference in enumerate(safety_docs):
            _check_manifest_path(
                reference, repo_root, f"manifest.safety_docs[{index}]", issues
            )

    if manifest_theme_path is not None and manifest_theme_path.is_file():
        try:
            _THEME_TOOL.load_and_validate(manifest_theme_path)
        except (OSError, _THEME_TOOL.ThemeProfileError) as exc:
            issues.append(
                ValidationIssue(
                    "manifest.customization_surfaces.appearance.profile", str(exc)
                )
            )

    if issues:
        raise AgentConfigError(issues)
    return manifest


def validate_compatibility_alias(repo_root: Path = REPO_ROOT) -> None:
    """Keep the historical example path equivalent to the named worked example."""
    repo_root = Path(repo_root).resolve()
    alias = repo_root / "agent-config" / COMPATIBILITY_ALIAS.name
    named = repo_root / "agent-config" / NAMED_PAY_YOURSELF_FIRST_EXAMPLE.name
    alias_value = _read_json(alias, str(alias))
    named_value = _read_json(named, str(named))
    if alias_value != named_value:
        raise AgentConfigError(
            [
                ValidationIssue(
                    "compatibility_alias",
                    f"{alias.name} must remain identical to {named.name}",
                )
            ]
        )


def _resolve_cli_path(raw_path: str, repo_root: Path) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate agent customization profiles, references, and project manifest."
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="Profile to validate; repeat for more than one. Defaults to all tracked profiles.",
    )
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA),
        help="Customization profile schema path.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Project manifest path.",
    )
    parser.add_argument(
        "--skip-manifest",
        action="store_true",
        help="Validate profiles only.",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help=(
            "Require every selected profile to be ready_for_implementation or "
            "implemented. Use this before starting implementation of a local profile."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    profile_paths = (
        tuple(_resolve_cli_path(value, REPO_ROOT) for value in args.profile)
        if args.profile
        else DEFAULT_PROFILES
    )
    schema_path = _resolve_cli_path(args.schema, REPO_ROOT)
    manifest_path = _resolve_cli_path(args.manifest, REPO_ROOT)
    issues: list[ValidationIssue] = []
    validated_profiles: list[str] = []

    for profile_path in profile_paths:
        try:
            validate_profile(
                profile_path,
                schema_path=schema_path,
                repo_root=REPO_ROOT,
                require_ready=args.require_ready,
            )
            validated_profiles.append(str(profile_path.relative_to(REPO_ROOT)))
        except AgentConfigError as exc:
            issues.extend(
                ValidationIssue(str(profile_path), issue.render()) for issue in exc.issues
            )

    manifest_validated = False
    if not args.skip_manifest:
        try:
            validate_manifest(manifest_path, repo_root=REPO_ROOT)
            validate_compatibility_alias(REPO_ROOT)
            manifest_validated = True
        except AgentConfigError as exc:
            issues.extend(exc.issues)

    if issues:
        payload = {
            "ok": False,
            "profiles": validated_profiles,
            "manifest_validated": manifest_validated,
            "errors": [issue.render() for issue in issues],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Agent configuration validation failed:", file=sys.stderr)
            for issue in issues:
                print(f"- {issue.render()}", file=sys.stderr)
        return 2

    payload = {
        "ok": True,
        "profiles": validated_profiles,
        "manifest_validated": manifest_validated,
        "errors": [],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"Validated {len(validated_profiles)} customization profile(s)"
            + (" and the project manifest." if manifest_validated else ".")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

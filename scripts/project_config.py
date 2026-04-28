"""Project configuration helpers for the orchestration runner.

This module isolates config loading, workflow policy normalization, and
validation so the main runner stays focused on orchestration flow.
"""

from __future__ import annotations

import json
import os


PRESET_TIER_ORDER = ("cheap", "default", "hard")
ROUTING_RULE_TASK_TYPES = {"issue", "pr"}

TRACKER_GITHUB = "github"
TRACKER_JIRA = "jira"
TRACKER_CHOICES = {TRACKER_GITHUB, TRACKER_JIRA}

CODEHOST_GITHUB = "github"
CODEHOST_BITBUCKET = "bitbucket"
CODEHOST_CUSTOM_PROXY = "custom-proxy"
CODEHOST_CHOICES = {CODEHOST_GITHUB, CODEHOST_BITBUCKET, CODEHOST_CUSTOM_PROXY}

WORKFLOW_COMMAND_ORDER = ["setup", "test", "lint", "build", "e2e"]
WORKFLOW_CHECK_COMMAND_ORDER = ["test", "lint", "build", "e2e"]
WORKFLOW_HOOK_NAMES = ["pre_agent", "post_agent", "pre_pr_update", "post_pr_update"]
WORKFLOW_HOOK_ALIASES = {
    "before_agent": "pre_agent",
    "after_agent": "post_agent",
    "before_pr_update": "pre_pr_update",
    "after_pr_update": "post_pr_update",
}
MERGE_METHOD_CHOICES = {"merge", "squash", "rebase"}


def _as_positive_int(value: object) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized < 1:
        return None
    return normalized


def _as_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def parse_tracker(tracker: object) -> str:
    normalized = str(tracker or "").strip().lower()
    if normalized not in TRACKER_CHOICES:
        raise RuntimeError(f"Unsupported tracker '{tracker}'. Expected one of: {', '.join(sorted(TRACKER_CHOICES))}")
    return normalized


def parse_codehost(codehost: object) -> str:
    normalized = str(codehost or "").strip().lower()
    if normalized not in CODEHOST_CHOICES:
        raise RuntimeError(
            f"Unsupported code host '{codehost}'. Expected one of: {', '.join(sorted(CODEHOST_CHOICES))}"
        )
    return normalized


def configured_workflow_commands(project_config: dict) -> list[tuple[str, str]]:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return []

    commands = workflow.get("commands")
    if not isinstance(commands, dict):
        return []

    configured: list[tuple[str, str]] = []
    for check_name in WORKFLOW_CHECK_COMMAND_ORDER:
        command = commands.get(check_name)
        if command is None:
            continue
        command_text = str(command).strip()
        if not command_text:
            continue
        configured.append((check_name, command_text))
    return configured


def configured_setup_command(project_config: dict) -> str | None:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return None

    commands = workflow.get("commands")
    if not isinstance(commands, dict):
        return None

    command = commands.get("setup")
    if not isinstance(command, str):
        return None
    normalized = command.strip()
    return normalized or None


def configured_setup_commands(project_config: dict) -> list[tuple[str, str]]:
    command = configured_setup_command(project_config)
    if command is None:
        return []
    return [("setup", command)]


def _normalize_hook_command_list(value: object, config_key: str | None = None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            if config_key is not None:
                raise RuntimeError(f"Project config key '{config_key}' must be a non-empty string or array of non-empty strings")
            return []
        return [normalized]
    if not isinstance(value, list):
        if config_key is not None:
            raise RuntimeError(f"Project config key '{config_key}' must be a string, array of strings, or null")
        return []

    normalized_commands: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            if config_key is not None:
                raise RuntimeError(f"Project config key '{config_key}' must contain non-empty strings")
            continue
        normalized_commands.append(item.strip())
    return normalized_commands


def configured_workflow_hooks(project_config: dict) -> dict[str, list[str]]:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return {}

    hooks = workflow.get("hooks")
    if not isinstance(hooks, dict):
        return {}

    configured: dict[str, list[str]] = {}
    for raw_hook_name, raw_value in hooks.items():
        hook_name = WORKFLOW_HOOK_ALIASES.get(str(raw_hook_name), str(raw_hook_name))
        if hook_name not in WORKFLOW_HOOK_NAMES:
            continue
        commands = _normalize_hook_command_list(raw_value)
        if commands:
            configured[hook_name] = commands
    return configured


def workflow_hooks(project_config: dict) -> dict[str, str]:
    configured: dict[str, str] = {}
    for hook_name, commands in configured_workflow_hooks(project_config).items():
        if commands:
            configured[hook_name] = commands[0]
    return configured


def workflow_readiness_policy(project_config: dict) -> dict[str, object]:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return {}

    readiness = workflow.get("readiness")
    if not isinstance(readiness, dict):
        return {}

    normalized: dict[str, object] = {}
    if "required_checks" in readiness and isinstance(readiness.get("required_checks"), list):
        required_checks: list[str] = []
        seen_checks: set[str] = set()
        for raw_name in readiness.get("required_checks") or []:
            name = str(raw_name or "").strip()
            key = name.lower()
            if not name or key in seen_checks:
                continue
            seen_checks.add(key)
            required_checks.append(name)
        normalized["required_checks"] = required_checks

    if "required_approvals" in readiness:
        approvals = _as_positive_int(readiness.get("required_approvals"))
        normalized["required_approvals"] = approvals if approvals is not None else 0

    if "require_review" in readiness:
        normalized["require_review"] = bool(readiness.get("require_review"))
    if "require_review_approval" in readiness:
        normalized["require_review"] = bool(readiness.get("require_review_approval"))
    if "require_mergeable" in readiness:
        normalized["require_mergeable"] = bool(readiness.get("require_mergeable"))
    if "require_required_file_evidence" in readiness:
        normalized["require_required_file_evidence"] = bool(readiness.get("require_required_file_evidence"))
    if "require_green_checks" in readiness:
        normalized["require_green_checks"] = bool(readiness.get("require_green_checks"))
    if "require_local_workflow_checks" in readiness:
        normalized["require_local_workflow_checks"] = bool(readiness.get("require_local_workflow_checks"))
    return normalized


def workflow_merge_policy(project_config: dict) -> dict[str, object]:
    workflow = project_config.get("workflow") if isinstance(project_config, dict) else None
    if not isinstance(workflow, dict):
        return {}

    merge = workflow.get("merge")
    if not isinstance(merge, dict):
        return {}

    normalized: dict[str, object] = {}
    if "auto" in merge:
        normalized["auto"] = bool(merge.get("auto"))
    if "auto_merge" in merge:
        normalized.setdefault("auto", bool(merge.get("auto_merge")))
    if "method" in merge:
        method = _as_optional_string(merge.get("method"))
        if method is not None:
            normalized["method"] = method
    return normalized


def _validate_project_workflow(config: dict, config_path: str) -> None:
    supported_workflow_keys = {"commands", "hooks", "readiness", "merge"}
    unsupported_workflow = sorted(set(config) - supported_workflow_keys)
    if unsupported_workflow:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'workflow': "
            + ", ".join(unsupported_workflow)
        )

    commands = config.get("commands")
    if commands is not None:
        if not isinstance(commands, dict):
            raise RuntimeError("Project config key 'workflow.commands' must be an object")

        supported_commands = set(WORKFLOW_COMMAND_ORDER)
        unsupported_commands = sorted(set(commands) - supported_commands)
        if unsupported_commands:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'workflow.commands': "
                + ", ".join(unsupported_commands)
            )

        for key in supported_commands:
            if key in commands and commands[key] is not None and not isinstance(commands[key], str):
                raise RuntimeError(
                    f"Project config key 'workflow.commands.{key}' must be a string or null"
                )
            if key in commands and isinstance(commands[key], str) and not commands[key].strip():
                raise RuntimeError(
                    f"Project config key 'workflow.commands.{key}' must be a non-empty string or null"
                )

        for key in supported_commands:
            if key in commands and commands[key] is not None and not isinstance(commands[key], str):
                raise RuntimeError(
                    f"Project config key 'workflow.commands.{key}' must be a string or null"
                )
            if key in commands and isinstance(commands[key], str) and not commands[key].strip():
                raise RuntimeError(
                    f"Project config key 'workflow.commands.{key}' must be a non-empty string or null"
                )

    hooks = config.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise RuntimeError("Project config key 'workflow.hooks' must be an object")
        supported_hooks = set(WORKFLOW_HOOK_NAMES) | set(WORKFLOW_HOOK_ALIASES)
        unsupported_hooks = sorted(set(hooks) - supported_hooks)
        if unsupported_hooks:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'workflow.hooks': "
                + ", ".join(unsupported_hooks)
            )

        for hook_name in supported_hooks:
            if hook_name in hooks:
                _normalize_hook_command_list(hooks.get(hook_name), f"workflow.hooks.{hook_name}")

    readiness = config.get("readiness")
    if readiness is not None:
        if not isinstance(readiness, dict):
            raise RuntimeError("Project config key 'workflow.readiness' must be an object")

        supported_readiness_keys = {
            "required_checks",
            "required_approvals",
            "require_review",
            "require_review_approval",
            "require_mergeable",
            "require_required_file_evidence",
            "require_green_checks",
            "require_local_workflow_checks",
        }
        unsupported_readiness = sorted(set(readiness) - supported_readiness_keys)
        if unsupported_readiness:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'workflow.readiness': "
                + ", ".join(unsupported_readiness)
            )

        required_checks = readiness.get("required_checks")
        if required_checks is not None:
            if not isinstance(required_checks, list):
                raise RuntimeError(
                    "Project config key 'workflow.readiness.required_checks' must be an array of strings"
                )
            for value in required_checks:
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(
                        "Project config key 'workflow.readiness.required_checks' must contain non-empty strings"
                    )

        if "required_approvals" in readiness:
            value = readiness.get("required_approvals")
            if type(value) is not int or value < 0:
                raise RuntimeError(
                    "Project config key 'workflow.readiness.required_approvals' must be a non-negative integer"
                )

        for key in [
            "require_review",
            "require_review_approval",
            "require_mergeable",
            "require_required_file_evidence",
            "require_green_checks",
            "require_local_workflow_checks",
        ]:
            if key in readiness and not isinstance(readiness.get(key), bool):
                raise RuntimeError(
                    f"Project config key 'workflow.readiness.{key}' must be a boolean"
                )

    merge = config.get("merge")
    if merge is not None:
        if not isinstance(merge, dict):
            raise RuntimeError("Project config key 'workflow.merge' must be an object")

        supported_merge_keys = {"auto", "auto_merge", "method"}
        unsupported_merge = sorted(set(merge) - supported_merge_keys)
        if unsupported_merge:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'workflow.merge': "
                + ", ".join(unsupported_merge)
            )

        if "auto" in merge and not isinstance(merge.get("auto"), bool):
            raise RuntimeError("Project config key 'workflow.merge.auto' must be a boolean")
        if "auto_merge" in merge and not isinstance(merge.get("auto_merge"), bool):
            raise RuntimeError("Project config key 'workflow.merge.auto_merge' must be a boolean")

        if "method" in merge:
            method = merge.get("method")
            if method is not None and method not in MERGE_METHOD_CHOICES:
                raise RuntimeError(
                    "Project config key 'workflow.merge.method' must be one of: merge, squash, rebase"
                )


def _validate_project_defaults(config: dict, config_path: str) -> None:
    supported_defaults_keys = {
        "tracker",
        "codehost",
        "runner",
        "agent",
        "model",
        "track_tokens",
        "token_budget",
        "preset",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "max_attempts",
    }
    unsupported_defaults = sorted(set(config) - supported_defaults_keys)
    if unsupported_defaults:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'defaults': "
            + ", ".join(unsupported_defaults)
        )

    if "tracker" in config:
        parse_tracker(config["tracker"])

    if "codehost" in config:
        parse_codehost(config["codehost"])

    if "runner" in config and config["runner"] not in {"claude", "opencode"}:
        raise RuntimeError("Project config key 'defaults.runner' must be one of: claude, opencode")

    if "agent" in config:
        value = config["agent"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Project config key 'defaults.agent' must be a non-empty string")

    if "model" in config and config["model"] is not None and not isinstance(config["model"], str):
        raise RuntimeError("Project config key 'defaults.model' must be a string or null")

    if "track_tokens" in config and not isinstance(config["track_tokens"], bool):
        raise RuntimeError("Project config key 'defaults.track_tokens' must be a boolean")

    if "token_budget" in config:
        value = config["token_budget"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Project config key 'defaults.token_budget' must be a positive integer or null"
            )

    if "preset" in config:
        value = config["preset"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Project config key 'defaults.preset' must be a non-empty string")

    if "agent_timeout_seconds" in config:
        value = config["agent_timeout_seconds"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(
                "Project config key 'defaults.agent_timeout_seconds' must be a positive integer"
            )

    if "agent_idle_timeout_seconds" in config:
        value = config["agent_idle_timeout_seconds"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                "Project config key 'defaults.agent_idle_timeout_seconds' must be a positive integer or null"
            )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(
                "Project config key 'defaults.max_attempts' must be a positive integer"
            )


def _validate_project_scope(config: dict, config_path: str) -> None:
    supported_scope_keys = {"defaults"}
    unsupported_scope = sorted(set(config) - supported_scope_keys)
    if unsupported_scope:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'scope': "
            + ", ".join(unsupported_scope)
        )

    if "defaults" in config and not isinstance(config["defaults"], dict):
        raise RuntimeError("Project config key 'scope.defaults' must be an object")

    defaults = config.get("defaults")
    if not isinstance(defaults, dict):
        return

    supported_defaults_keys = {"labels", "authors", "assignees", "priority", "freshness"}
    unsupported_defaults = sorted(set(defaults) - supported_defaults_keys)
    if unsupported_defaults:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'scope.defaults': "
            + ", ".join(unsupported_defaults)
        )

    for section_key in ["labels", "authors", "assignees", "priority"]:
        section = defaults.get(section_key)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise RuntimeError(
                f"Project config key 'scope.defaults.{section_key}' must be an object"
            )

        supported_section_keys = {"allow", "deny"}
        if section_key == "priority":
            supported_section_keys = {"allow", "deny", "order"}
        unsupported_section = sorted(set(section) - supported_section_keys)
        if unsupported_section:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'scope.defaults.{section_key}': "
                + ", ".join(unsupported_section)
            )

        for rule_key in sorted(supported_section_keys):
            values = section.get(rule_key)
            if values is None:
                continue
            if not isinstance(values, list):
                raise RuntimeError(
                    f"Project config key 'scope.defaults.{section_key}.{rule_key}' must be an array of strings"
                )
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise RuntimeError(
                        f"Project config key 'scope.defaults.{section_key}.{rule_key}' must contain non-empty strings"
                    )

    freshness = defaults.get("freshness")
    if freshness is not None:
        if not isinstance(freshness, dict):
            raise RuntimeError("Project config key 'scope.defaults.freshness' must be an object")
        supported_freshness_keys = {"max_age_days", "max_idle_days"}
        unsupported_freshness = sorted(set(freshness) - supported_freshness_keys)
        if unsupported_freshness:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under 'scope.defaults.freshness': "
                + ", ".join(unsupported_freshness)
            )
        for rule_key in sorted(supported_freshness_keys):
            value = freshness.get(rule_key)
            if value is None:
                continue
            if type(value) is not int or value <= 0:
                raise RuntimeError(
                    f"Project config key 'scope.defaults.freshness.{rule_key}' must be a positive integer"
                )


def _validate_project_routing(config: dict, config_path: str) -> None:
    supported_routing_keys = {"default_preset", "rules"}
    unsupported_routing = sorted(set(config) - supported_routing_keys)
    if unsupported_routing:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'routing': "
            + ", ".join(unsupported_routing)
        )

    if "default_preset" in config:
        value = config["default_preset"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Project config key 'routing.default_preset' must be a non-empty string")

    if "rules" not in config:
        return

    rules = config["rules"]
    if not isinstance(rules, list):
        raise RuntimeError("Project config key 'routing.rules' must be an array")

    for index, rule in enumerate(rules):
        prefix = f"routing.rules[{index}]"
        if not isinstance(rule, dict):
            raise RuntimeError(f"Project config key '{prefix}' must be an object")
        unsupported_rule = sorted(set(rule) - {"when", "preset"})
        if unsupported_rule:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under '{prefix}': "
                + ", ".join(unsupported_rule)
            )

        preset_value = rule.get("preset")
        if not isinstance(preset_value, str) or not preset_value.strip():
            raise RuntimeError(f"Project config key '{prefix}.preset' must be a non-empty string")

        when = rule.get("when")
        if not isinstance(when, dict):
            raise RuntimeError(f"Project config key '{prefix}.when' must be an object")

        unsupported_when = sorted(set(when) - {"labels", "task_types", "scope", "needs_decomposition"})
        if unsupported_when:
            raise RuntimeError(
                f"Unsupported key(s) in project config {config_path} under '{prefix}.when': "
                + ", ".join(unsupported_when)
            )

        for list_key in ["labels", "task_types"]:
            if list_key not in when:
                continue
            values = when[list_key]
            if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
                raise RuntimeError(f"Project config key '{prefix}.when.{list_key}' must be an array of non-empty strings")
            if list_key == "task_types":
                invalid = sorted({str(item) for item in values if str(item).strip().lower() not in ROUTING_RULE_TASK_TYPES})
                if invalid:
                    raise RuntimeError(
                        f"Project config key '{prefix}.when.task_types' supports only: "
                        + ", ".join(sorted(ROUTING_RULE_TASK_TYPES))
                    )

        if "scope" in when:
            scope_value = when["scope"]
            if scope_value not in {"in", "out"}:
                raise RuntimeError(f"Project config key '{prefix}.when.scope' must be one of: in, out")

        if "needs_decomposition" in when and not isinstance(when["needs_decomposition"], bool):
            raise RuntimeError(f"Project config key '{prefix}.when.needs_decomposition' must be a boolean")


def _validate_project_budgets(config: dict, config_path: str) -> None:
    supported_budget_keys = {"max_attempts_per_task", "max_runtime_minutes", "max_cost_usd", "max_model_tier"}
    unsupported = sorted(set(config) - supported_budget_keys)
    if unsupported:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'budgets': "
            + ", ".join(unsupported)
        )

    for int_key in ["max_attempts_per_task", "max_runtime_minutes"]:
        if int_key in config and (type(config[int_key]) is not int or config[int_key] <= 0):
            raise RuntimeError(f"Project config key 'budgets.{int_key}' must be a positive integer")

    if "max_cost_usd" in config:
        value = config["max_cost_usd"]
        if not isinstance(value, (int, float)) or value <= 0:
            raise RuntimeError("Project config key 'budgets.max_cost_usd' must be a positive number")

    if "max_model_tier" in config:
        value = _as_optional_string(config["max_model_tier"])
        if value not in PRESET_TIER_ORDER:
            raise RuntimeError("Project config key 'budgets.max_model_tier' must be one of: cheap, default, hard")


def _validate_project_retry(config: dict, config_path: str) -> None:
    supported_retry_keys = {"max_attempts", "escalate_to_preset"}
    unsupported_retry = sorted(set(config) - supported_retry_keys)
    if unsupported_retry:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'retry': "
            + ", ".join(unsupported_retry)
        )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError("Project config key 'retry.max_attempts' must be a positive integer")

    if "escalate_to_preset" in config:
        value = config["escalate_to_preset"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RuntimeError(
                "Project config key 'retry.escalate_to_preset' must be a non-empty string or null"
            )


def _validate_preset_config(config: dict, config_path: str, prefix: str) -> None:
    supported_preset_keys = {
        "runner",
        "agent",
        "model",
        "track_tokens",
        "token_budget",
        "agent_timeout_seconds",
        "agent_idle_timeout_seconds",
        "max_attempts",
        "escalate_to_preset",
    }
    unsupported = sorted(set(config) - supported_preset_keys)
    if unsupported:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under '{prefix}': "
            + ", ".join(unsupported)
        )

    if "runner" in config and config["runner"] not in {"claude", "opencode"}:
        raise RuntimeError(f"Project config key '{prefix}.runner' must be one of: claude, opencode")

    if "agent" in config:
        value = config["agent"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Project config key '{prefix}.agent' must be a non-empty string")

    if "model" in config and config["model"] is not None and not isinstance(config["model"], str):
        raise RuntimeError(f"Project config key '{prefix}.model' must be a string or null")

    if "track_tokens" in config and not isinstance(config["track_tokens"], bool):
        raise RuntimeError(f"Project config key '{prefix}.track_tokens' must be a boolean")

    if "token_budget" in config:
        value = config["token_budget"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(f"Project config key '{prefix}.token_budget' must be a positive integer or null")

    if "agent_timeout_seconds" in config:
        value = config["agent_timeout_seconds"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(f"Project config key '{prefix}.agent_timeout_seconds' must be a positive integer")

    if "agent_idle_timeout_seconds" in config:
        value = config["agent_idle_timeout_seconds"]
        if value is not None and (type(value) is not int or value <= 0):
            raise RuntimeError(
                f"Project config key '{prefix}.agent_idle_timeout_seconds' must be a positive integer or null"
            )

    if "max_attempts" in config:
        value = config["max_attempts"]
        if type(value) is not int or value <= 0:
            raise RuntimeError(f"Project config key '{prefix}.max_attempts' must be a positive integer")

    if "escalate_to_preset" in config:
        value = config["escalate_to_preset"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RuntimeError(
                f"Project config key '{prefix}.escalate_to_preset' must be a non-empty string or null"
            )


def _validate_project_presets(config: dict, config_path: str) -> None:
    for preset_name, preset_config in config.items():
        if not isinstance(preset_name, str) or not preset_name.strip():
            raise RuntimeError(
                f"Project config {config_path} preset names must be non-empty strings"
            )
        if not isinstance(preset_config, dict):
            raise RuntimeError(
                f"Project config key 'presets.{preset_name}' must be an object"
            )
        _validate_preset_config(preset_config, config_path, f"presets.{preset_name}")


def _validate_retry_references(config: dict, preset_names: set[str], config_path: str, prefix: str) -> None:
    target = _as_optional_string(config.get("escalate_to_preset"))
    if target is not None and target not in preset_names:
        raise RuntimeError(
            f"Project config key '{prefix}.escalate_to_preset' references unknown preset '{target}'"
        )


def _validate_project_communication(config: dict, config_path: str) -> None:
    supported_communication_keys = {"verbosity"}
    unsupported_communication = sorted(set(config) - supported_communication_keys)
    if unsupported_communication:
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path} under 'communication': "
            + ", ".join(unsupported_communication)
        )

    if "verbosity" in config and config["verbosity"] not in {"low", "normal", "high"}:
        raise RuntimeError(
            "Project config key 'communication.verbosity' must be one of: low, normal, high"
        )


def validate_project_config(config: dict, config_path: str) -> dict:
    supported_top_level_keys = {
        "workflow",
        "defaults",
        "scope",
        "routing",
        "retry",
        "budgets",
        "communication",
        "presets",
    }

    unsupported = sorted(set(config) - supported_top_level_keys)
    if unsupported:
        unsupported_text = ", ".join(unsupported)
        raise RuntimeError(
            f"Unsupported key(s) in project config {config_path}: {unsupported_text}"
        )

    for key in ["workflow", "defaults", "scope", "routing", "retry", "budgets", "communication", "presets"]:
        if key in config and not isinstance(config[key], dict):
            raise RuntimeError(f"Project config key '{key}' must be an object")

    workflow = config.get("workflow")
    if isinstance(workflow, dict):
        _validate_project_workflow(workflow, config_path)

    defaults = config.get("defaults")
    if isinstance(defaults, dict):
        _validate_project_defaults(defaults, config_path)

    scope = config.get("scope")
    if isinstance(scope, dict):
        _validate_project_scope(scope, config_path)

    routing = config.get("routing")
    if isinstance(routing, dict):
        _validate_project_routing(routing, config_path)

    retry = config.get("retry")
    if isinstance(retry, dict):
        _validate_project_retry(retry, config_path)

    budgets = config.get("budgets")
    if isinstance(budgets, dict):
        _validate_project_budgets(budgets, config_path)

    communication = config.get("communication")
    if isinstance(communication, dict):
        _validate_project_communication(communication, config_path)

    presets = config.get("presets")
    if isinstance(presets, dict):
        _validate_project_presets(presets, config_path)

    preset_names = set(presets) if isinstance(presets, dict) else set()
    if isinstance(defaults, dict):
        default_preset = _as_optional_string(defaults.get("preset"))
        if default_preset is not None and default_preset not in preset_names:
            raise RuntimeError(
                f"Project config key 'defaults.preset' references unknown preset '{default_preset}'"
            )
    if isinstance(routing, dict):
        routing_default_preset = _as_optional_string(routing.get("default_preset"))
        if routing_default_preset is not None and routing_default_preset not in preset_names:
            raise RuntimeError(
                f"Project config key 'routing.default_preset' references unknown preset '{routing_default_preset}'"
            )
        routing_rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
        for index, rule in enumerate(routing_rules):
            if not isinstance(rule, dict):
                continue
            routed_preset = _as_optional_string(rule.get("preset"))
            if routed_preset is not None and routed_preset not in preset_names:
                raise RuntimeError(
                    f"Project config key 'routing.rules[{index}].preset' references unknown preset '{routed_preset}'"
                )

    if isinstance(retry, dict):
        _validate_retry_references(retry, preset_names, config_path, "retry")
    if isinstance(presets, dict):
        for preset_name, preset_config in presets.items():
            if isinstance(preset_config, dict):
                _validate_retry_references(
                    preset_config,
                    preset_names,
                    config_path,
                    f"presets.{preset_name}",
                )

    return config


def load_project_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, encoding="utf-8") as config_file:
            data = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in project config {config_path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot read project config {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Project config {config_path} must contain a JSON object")

    return validate_project_config(config=data, config_path=config_path)


def project_cli_defaults(project_config: dict) -> dict:
    defaults = project_config.get("defaults")
    cli_defaults: dict = {}
    if isinstance(defaults, dict):
        for key in [
            "tracker",
            "codehost",
            "runner",
            "agent",
            "model",
            "track_tokens",
            "token_budget",
            "preset",
            "agent_timeout_seconds",
            "agent_idle_timeout_seconds",
            "max_attempts",
        ]:
            if key in defaults:
                cli_defaults[key] = defaults[key]

    retry = project_config.get("retry")
    if isinstance(retry, dict):
        for key in ["max_attempts", "escalate_to_preset"]:
            if key in retry:
                cli_defaults[key] = retry[key]
    return cli_defaults

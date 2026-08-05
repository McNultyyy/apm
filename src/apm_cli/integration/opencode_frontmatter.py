"""Canonical translation of portable agent frontmatter to OpenCode."""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from apm_cli.utils.yaml_io import load_yaml_str, yaml_to_str

# OpenCode theme color enum (see sst/opencode config schema).
OPENCODE_THEME_COLORS = frozenset(
    {"primary", "secondary", "accent", "success", "warning", "error", "info"}
)

# Hex color regex: #RGB or #RRGGBB, case-insensitive.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n)?",
    re.DOTALL,
)
_MODEL_PROVIDER_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")

_OPENCODE_FRONTMATTER_KEYS = (
    "name",
    "description",
    "temperature",
    "top_p",
    "prompt",
    "permission",
    "disable",
    "hidden",
    "steps",
)
_OPENCODE_MODES = frozenset({"primary", "subagent", "all"})
_OPENCODE_TOOL_ALIASES = {
    "bash": "bash",
    "edit": "edit",
    "execute": "bash",
    "fetch": "webfetch",
    "glob": "glob",
    "grep": "grep",
    "read": "read",
    "search": "grep",
    "shell": "bash",
    "task": "task",
    "agent": "task",
    "web": "webfetch",
    "webfetch": "webfetch",
    "write": "write",
}
_MODEL_PROVIDER_ALIASES = {
    "copilot": "github-copilot",
    "github copilot": "github-copilot",
    "github-copilot": "github-copilot",
}


@dataclass(frozen=True)
class OpencodeAgentTranslation:
    """Rendered OpenCode content plus user-visible lossy-conversion notices."""

    content: str
    notices: tuple[str, ...] = ()


class OpencodeAgentTranslationError(ValueError):
    """Raised when source intent cannot be represented safely in OpenCode."""


def translate_opencode_agent(content: str) -> OpencodeAgentTranslation:
    """Render portable Markdown agent content in OpenCode's native schema.

    Unknown frontmatter keys and invalid optional values are omitted with a
    notice. Unsupported tools and malformed metadata fail closed. Portable
    tool lists, comma-separated strings, and boolean mappings become
    OpenCode's ``Record<string, boolean>`` shape. Model display names and bare
    model IDs resolve to ``provider/model`` identifiers.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return OpencodeAgentTranslation(content)

    try:
        metadata = load_yaml_str(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise OpencodeAgentTranslationError(
            "frontmatter is invalid YAML; fix the YAML syntax"
        ) from exc
    if not isinstance(metadata, dict):
        raise OpencodeAgentTranslationError(
            "frontmatter must be a YAML mapping; replace the top-level value with key/value fields"
        )

    notices: list[str] = []
    translated: dict[str, object] = {}
    for key in _OPENCODE_FRONTMATTER_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        if _is_valid_supported_value(key, value):
            translated[key] = value
        else:
            notices.append(f"dropped invalid '{key}' value")

    ignored_keys = sorted(
        str(key)
        for key in metadata
        if key not in {*_OPENCODE_FRONTMATTER_KEYS, "mode", "model", "tools", "color"}
    )
    if ignored_keys:
        notices.append(f"dropped unsupported fields: {', '.join(ignored_keys)}")

    mode = metadata.get("mode")
    if not isinstance(mode, str) or mode not in _OPENCODE_MODES:
        if "mode" in metadata:
            notices.append("replaced invalid 'mode' with 'subagent'")
        mode = "subagent"
    translated["mode"] = mode

    model = _translate_model(metadata.get("model"))
    if model is not None:
        translated["model"] = model
    elif "model" in metadata:
        notices.append("dropped invalid 'model' value")

    tools = _translate_tools(metadata.get("tools"))
    if tools:
        translated["tools"] = tools

    color = metadata.get("color")
    if _is_valid_opencode_color(color):
        translated["color"] = color
    elif "color" in metadata:
        notices.append("dropped invalid 'color' value")

    body = content[match.end() :].lstrip("\r\n")
    rendered_frontmatter = yaml_to_str(translated, sort_keys=False).rstrip()
    rendered = f"---\n{rendered_frontmatter}\n---\n\n{body}"
    return OpencodeAgentTranslation(rendered, tuple(notices))


def _translate_model(value: object) -> str | None:
    """Return an OpenCode ``provider/model`` identifier."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if "/" in value:
        return value

    provider = "github-copilot"
    provider_match = _MODEL_PROVIDER_RE.match(value)
    if provider_match:
        value = provider_match.group(1).strip()
        provider_name = provider_match.group(2).strip().lower()
        provider = _MODEL_PROVIDER_ALIASES.get(provider_name, _slugify(provider_name))

    model = _slugify(value)
    if not model or not provider:
        return None
    return f"{provider}/{model}"


def _translate_tools(value: object) -> dict[str, bool]:
    """Map portable tools, failing closed when any tool is unrepresentable."""
    if value is None:
        return {}

    entries: list[tuple[object, object]]
    if isinstance(value, dict):
        entries = list(value.items())
    elif isinstance(value, list):
        entries = [(item, True) for item in value]
    elif isinstance(value, str):
        entries = [(item.strip(), True) for item in value.split(",")]
    else:
        raise OpencodeAgentTranslationError(
            "'tools' must be a list, comma-separated string, or boolean mapping"
        )

    translated: dict[str, bool] = {}
    unsupported: list[str] = []
    for source_name, enabled in entries:
        if not isinstance(source_name, str) or not isinstance(enabled, bool):
            raise OpencodeAgentTranslationError(
                "'tools' entries must use string names and boolean values"
            )
        native_name = _OPENCODE_TOOL_ALIASES.get(source_name.strip().lower())
        if native_name is None:
            unsupported.append(source_name)
            continue
        translated[native_name] = translated.get(native_name, True) and enabled
    if unsupported:
        values = ", ".join(sorted(unsupported))
        approved = ", ".join(sorted(_OPENCODE_TOOL_ALIASES))
        raise OpencodeAgentTranslationError(
            f"unsupported tools: {values}; use only approved names: {approved}"
        )
    return translated


def _slugify(value: str) -> str:
    """Normalize a model or provider display name to an identifier segment."""
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")


def _is_valid_supported_value(key: str, value: object) -> bool:
    """Return whether a pass-through field has the OpenCode schema shape."""
    if key in {"name", "description", "prompt"}:
        return isinstance(value, str)
    if key in {"temperature", "top_p"}:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if key == "permission":
        return isinstance(value, dict)
    if key in {"disable", "hidden"}:
        return isinstance(value, bool)
    if key == "steps":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    return False


def _is_valid_opencode_color(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in OPENCODE_THEME_COLORS:
        return True
    return bool(_HEX_COLOR_RE.match(value))

"""Canonical translation of portable agent frontmatter to OpenCode."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.yaml_io import load_yaml_str, yaml_to_str

# OpenCode theme color enum (see sst/opencode config schema).
OPENCODE_THEME_COLORS = frozenset(
    {"primary", "secondary", "accent", "success", "warning", "error", "info"}
)

# Hex color regex: #RGB or #RRGGBB, case-insensitive.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
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


def translate_opencode_agent(content: str) -> str:
    """Render portable Markdown agent content in OpenCode's native schema.

    Unknown frontmatter keys and unsupported tools are dropped. Portable tool
    lists, comma-separated strings, and boolean mappings become OpenCode's
    ``Record<string, boolean>`` shape. Model display names and bare model IDs
    resolve to ``provider/model`` identifiers.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return content

    try:
        metadata = load_yaml_str(match.group(1)) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    translated = {key: metadata[key] for key in _OPENCODE_FRONTMATTER_KEYS if key in metadata}

    mode = metadata.get("mode")
    if not isinstance(mode, str) or mode not in _OPENCODE_MODES:
        mode = "subagent"
    translated["mode"] = mode

    model = _translate_model(metadata.get("model"))
    if model is not None:
        translated["model"] = model

    tools = _translate_tools(metadata.get("tools"))
    if tools:
        translated["tools"] = tools

    color = metadata.get("color")
    if _is_valid_opencode_color(color):
        translated["color"] = color

    body = content[match.end() :]
    rendered_frontmatter = yaml_to_str(translated, sort_keys=False).rstrip()
    return f"---\n{rendered_frontmatter}\n---\n\n{body.lstrip()}"


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
    """Map portable tool declarations to OpenCode's native vocabulary."""
    entries: list[tuple[object, object]]
    if isinstance(value, dict):
        entries = list(value.items())
    elif isinstance(value, list):
        entries = [(item, True) for item in value]
    elif isinstance(value, str):
        entries = [(item.strip(), True) for item in value.split(",")]
    else:
        return {}

    translated: dict[str, bool] = {}
    for source_name, enabled in entries:
        if not isinstance(source_name, str) or not isinstance(enabled, bool):
            continue
        native_name = _OPENCODE_TOOL_ALIASES.get(source_name.strip().lower())
        if native_name is not None:
            translated[native_name] = enabled
    return translated


def _slugify(value: str) -> str:
    """Normalize a model or provider display name to an identifier segment."""
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")


def _ascii_repr(value: object) -> str:
    """Return an ASCII-only repr of ``value``.

    Drop-in replacement for ``!r`` interpolation: ``ascii()`` escapes
    any non-ASCII codepoints (e.g. ``'cy\\xe1n'``) so diagnostics never
    leak raw non-ASCII bytes from user frontmatter into CLI output.
    """
    return ascii(value)


def validate_opencode_frontmatter(
    fm: dict | None,
    source: Path,
    package_name: str | None = None,
) -> list[str]:
    """Return ASCII warning messages for OpenCode-incompatible fields.

    Empty list means no incompatibilities were detected. The caller
    is responsible for surfacing each message via the diagnostics
    collector; this function is pure.

    Args:
        fm: Parsed YAML frontmatter (may be ``None`` or empty dict).
        source: Source agent file path, used in messages so the user
            can locate the offending file.
        package_name: Optional owning APM package name. When provided,
            the warning identifies the agent as ``<pkg>/<file>`` so
            users running multi-package installs can tell which
            dependency the bad frontmatter came from.
    """
    if not fm or not isinstance(fm, dict):
        return []

    messages: list[str] = []
    safe_name = printable_ascii_text(source.name)
    identifier = f"{printable_ascii_text(package_name)}/{safe_name}" if package_name else safe_name

    if "tools" in fm:
        tools = fm["tools"]
        if not isinstance(tools, dict):
            kind = type(tools).__name__
            messages.append(
                f"OpenCode agent '{identifier}' has tools as {kind}; "
                "OpenCode requires a mapping of tool-name to boolean. "
                "OpenCode will reject this agent at load time. "
                "Fix: rewrite the frontmatter as 'tools: {Read: true, Grep: true}'."
            )
        else:
            for key, value in tools.items():
                if not isinstance(key, str) or not isinstance(value, bool):
                    messages.append(
                        f"OpenCode agent '{identifier}' has a non-boolean tool entry "
                        f"({_ascii_repr(key)}: {_ascii_repr(value)}); OpenCode requires "
                        "string-keyed boolean values. "
                        "OpenCode will reject this agent at load time. "
                        "Fix: set every tool entry to 'true' or 'false' "
                        "(e.g. 'Read: true')."
                    )
                    break

    if "color" in fm:
        color = fm["color"]
        if not _is_valid_opencode_color(color):
            messages.append(
                f"OpenCode agent '{identifier}' has color={_ascii_repr(color)}; "
                "OpenCode requires a hex value (e.g. '#aabbcc') or one of "
                f"{sorted(OPENCODE_THEME_COLORS)}. "
                "OpenCode will reject this agent at load time. "
                "Fix: replace the color with a '#rgb' or '#rrggbb' hex literal "
                "or one of the listed theme names."
            )

    return messages


def _is_valid_opencode_color(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in OPENCODE_THEME_COLORS:
        return True
    return bool(_HEX_COLOR_RE.match(value))

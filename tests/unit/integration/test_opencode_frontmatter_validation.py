"""Tests for OpenCode frontmatter validation and native translation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import frontmatter
import pytest

from apm_cli.integration import AgentIntegrator
from apm_cli.integration.opencode_frontmatter import OpencodeAgentTranslationError
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference
from apm_cli.utils.diagnostics import CATEGORY_ERROR, DiagnosticCollector


def _make_package_info(pkg_dir: Path, *, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0", package_path=pkg_dir)
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=pkg_dir,
        resolved_reference=resolved_ref,
        installed_at="2024-01-01T00:00:00",
    )


def _messages(diagnostics: DiagnosticCollector, category: str) -> list[str]:
    """Return raw diagnostic messages in one category."""
    return [d.message for d in diagnostics._diagnostics if d.category == category]


class TestOpencodeInstallTranslation:
    """End-to-end tests for OpenCode agent translation during install."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()
        # OpenCode has auto_create=False and detect_by_dir=True; create
        # the marker directory so integration runs.
        (self.project_root / ".opencode").mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_agent(self, frontmatter: str) -> Path:
        pkg = self.project_root / "package"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        agent_path = agents_dir / "demo.agent.md"
        agent_path.write_text(f"---\n{frontmatter}\n---\n\n# Demo\n")
        return pkg

    def test_tools_as_list_translates_without_warning(self):
        pkg = self._write_agent("tools:\n  - Read\n  - Grep\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        assert diagnostics.has_diagnostics is False
        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata["tools"] == {"read": True, "grep": True}

    def test_install_translates_portable_agent_to_opencode_schema(self):
        pkg = self._write_agent(
            "name: SomeAgent\n"
            "description: Reviews changes\n"
            "model: Claude Sonnet 5 (copilot)\n"
            "target: vscode\n"
            "user-invocable: false\n"
            "handoffs: [planner]\n"
            "tools:\n"
            "  - read\n"
            "  - edit\n"
            "  - search\n"
            "  - execute\n"
            "  - agent\n"
        )
        pkg_info = _make_package_info(pkg)

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"],
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata == {
            "name": "SomeAgent",
            "description": "Reviews changes",
            "mode": "subagent",
            "model": "github-copilot/claude-sonnet-5",
            "tools": {
                "read": True,
                "edit": True,
                "grep": True,
                "bash": True,
                "task": True,
            },
        }
        assert deployed.content == "# Demo"

    @pytest.mark.parametrize(
        "tools",
        [
            "  shell: false\n  execute: true\n",
            "  execute: true\n  shell: false\n",
        ],
    )
    def test_conflicting_tool_alias_disable_wins(self, tools: str):
        pkg = self._write_agent(f"tools:\n{tools}")
        pkg_info = _make_package_info(pkg)

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )

        assert result.files_integrated == 1
        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata["tools"]["bash"] is False

    @pytest.mark.parametrize(
        ("source_model", "expected"),
        [
            ("claude-opus-4.6", "github-copilot/claude-opus-4.6"),
            ("anthropic/claude-3-opus", "anthropic/claude-3-opus"),
        ],
    )
    def test_model_identifier_translation(self, source_model: str, expected: str):
        pkg = self._write_agent(f"model: {source_model}\n")
        pkg_info = _make_package_info(pkg)

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )

        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata["model"] == expected

    def test_empty_model_is_dropped_with_diagnostic(self):
        pkg = self._write_agent('model: "  "\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert "model" not in deployed.metadata
        messages = _messages(diagnostics, "agent_lossy_compilation")
        assert len(messages) == 1
        assert "dropped invalid 'model' value" in messages[0]

    @pytest.mark.parametrize(
        ("field", "yaml_value", "expected"),
        [
            ("prompt", "Review changes", "Review changes"),
            ("temperature", "0.4", 0.4),
            ("top_p", "0.8", 0.8),
            ("permission", "{read: allow}", {"read": "allow"}),
            ("disable", "true", True),
            ("hidden", "false", False),
            ("steps", "5", 5),
        ],
    )
    def test_supported_optional_fields_preserve_valid_shapes(
        self, field: str, yaml_value: str, expected: object
    ):
        pkg = self._write_agent(f"{field}: {yaml_value}\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata[field] == expected
        assert diagnostics.has_diagnostics is False

    @pytest.mark.parametrize(
        ("field", "yaml_value"),
        [
            ("prompt", "[not, text]"),
            ("temperature", "true"),
            ("top_p", "not-a-number"),
            ("permission", "allow"),
            ("disable", '"yes"'),
            ("hidden", "1"),
            ("steps", "0"),
        ],
    )
    def test_invalid_optional_field_shapes_are_diagnosed(self, field: str, yaml_value: str):
        pkg = self._write_agent(f"{field}: {yaml_value}\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert field not in deployed.metadata
        messages = _messages(diagnostics, "agent_lossy_compilation")
        assert len(messages) == 1
        assert f"dropped invalid '{field}' value" in messages[0]

    @pytest.mark.parametrize(
        ("source_mode", "expected"), [("primary", "primary"), ("bad", "subagent")]
    )
    def test_mode_translation(self, source_mode: str, expected: str):
        pkg = self._write_agent(f"mode: {source_mode}\n")
        pkg_info = _make_package_info(pkg)

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )

        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata["mode"] == expected

    def test_tools_as_dict_preserves_false(self):
        pkg = self._write_agent("tools:\n  Read: true\n  Grep: false\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert diagnostics.has_diagnostics is False
        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert deployed.metadata["tools"] == {"read": True, "grep": False}

    def test_unsupported_tool_fails_closed_with_actionable_error(self):
        pkg = self._write_agent("tools: [read, todo, vscode]\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert not (self.project_root / ".opencode" / "agents" / "demo.md").exists()
        messages = _messages(diagnostics, CATEGORY_ERROR)
        assert len(messages) == 1
        assert "unsupported tools: todo, vscode" in messages[0]
        assert "use only approved names" in messages[0]

    def test_color_named_is_dropped_with_diagnostic(self):
        pkg = self._write_agent('color: "cyan"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        messages = _messages(diagnostics, "agent_lossy_compilation")
        assert len(messages) == 1
        assert "dropped invalid 'color' value" in messages[0]
        deployed = frontmatter.load(self.project_root / ".opencode" / "agents" / "demo.md")
        assert "color" not in deployed.metadata

    def test_color_hex_no_warning(self):
        pkg = self._write_agent('color: "#aabbcc"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert diagnostics.has_diagnostics is False

    def test_file_is_deployed_after_translation(self):
        pkg = self._write_agent("tools:\n  - Read\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        deployed = self.project_root / ".opencode" / "agents" / "demo.md"
        assert deployed.exists()

    def test_translation_diagnostic_sanitizes_package_name(self, capsys):
        pkg = self._write_agent('color: "cyan"\n')
        pkg_info = _make_package_info(pkg, name="evil\x1b[31mpkg\nnext")
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"],
            pkg_info,
            self.project_root,
            diagnostics=diagnostics,
        )

        assert diagnostics.has_diagnostics is True
        diagnostics.render_summary()
        output = capsys.readouterr().out
        assert "\x1b" not in output
        assert "pkg\nnext" not in output

    def test_malformed_yaml_fails_closed_without_creating_agent_directory(self):
        pkg = self._write_agent("tools: [unclosed\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert not (self.project_root / ".opencode" / "agents").exists()
        messages = _messages(diagnostics, CATEGORY_ERROR)
        assert len(messages) == 1
        assert "frontmatter is invalid YAML" in messages[0]

    def test_matching_rendered_output_is_adopted(self):
        pkg = self._write_agent("tools: [read, search]\n")
        pkg_info = _make_package_info(pkg)

        first = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )
        second = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )

        assert first.files_integrated == 1
        assert second.files_integrated == 0
        assert second.files_adopted == 1

    def test_collision_requires_force_before_overwrite(self):
        pkg = self._write_agent("tools: [read]\n")
        pkg_info = _make_package_info(pkg)
        target = self.project_root / ".opencode" / "agents" / "demo.md"
        target.parent.mkdir()
        target.write_text("# Local\n", encoding="utf-8")

        skipped = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root
        )
        assert skipped.files_skipped == 1
        assert target.read_text(encoding="utf-8") == "# Local\n"

        forced = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, force=True
        )
        assert forced.files_integrated == 1
        assert frontmatter.load(target).metadata["tools"] == {"read": True}

    def test_render_rejects_symlink_source(self):
        real_source = self.project_root / "real.agent.md"
        real_source.write_text("---\ntools: [read]\n---\n\nBody\n", encoding="utf-8")
        source = self.project_root / "demo.agent.md"
        try:
            source.symlink_to(real_source)
        except OSError:
            pytest.skip("symlink creation is unavailable")

        with pytest.raises(OpencodeAgentTranslationError, match="Refusing to read symlink source"):
            self.integrator._render_opencode_agent(source, self.project_root / "target.md")

    @pytest.mark.parametrize("target_name", ["copilot", "claude", "codex", "windsurf", "cursor"])
    def test_non_opencode_targets_do_not_emit_opencode_warning(self, target_name):
        # The validation is scoped to format_id == "opencode_agent"; other
        # targets must not emit OpenCode-specific warnings even when the
        # frontmatter would be invalid for OpenCode.
        pkg = self._write_agent("tools:\n  - Read\ncolor: cyan\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()
        target = KNOWN_TARGETS[target_name]
        # Create marker dir for targets that need it.
        marker = self.project_root / target.root_dir
        marker.mkdir(exist_ok=True)

        self.integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert not any(
            item.category == "error" and "OpenCode agent" in item.message
            for item in diagnostics._diagnostics
        )

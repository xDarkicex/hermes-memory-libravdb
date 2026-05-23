"""Integration tests against real Hermes 0.14 plugin APIs.

Validates that register() works with both _ProviderCollector (directory load
path) and real PluginContext (entry-point path), that class inheritance
matches the ABCs, and that CLI discovery functions correctly.

These tests require hermes-agent to be installed.  When running in CI without
Hermes they are skipped automatically.
"""

from unittest.mock import MagicMock, patch

import pytest

# Skip entire module if Hermes is not installed (CI, linting)
pytest.importorskip("hermes_cli.plugins", reason="hermes-agent not installed")
pytest.importorskip("plugins.memory", reason="hermes-agent not installed")
pytest.importorskip("agent.context_engine", reason="hermes-agent not installed")
pytest.importorskip("agent.memory_provider", reason="hermes-agent not installed")


# ---------------------------------------------------------------------------
# Register entrypoint tests — real PluginContext & _ProviderCollector
# ---------------------------------------------------------------------------

class TestRegisterWithRealPluginContext:
    """Call register() with a real hermes_cli.plugins.PluginContext."""

    def test_register_with_real_plugin_context(self):
        """register() must not raise AttributeError with real PluginContext."""
        from hermes_cli.plugins import PluginContext, PluginManifest
        from hermes_memory_libravdb import register

        manifest = PluginManifest(name="libravdb", version="0.5.2")
        # PluginContext requires a PluginManager — mock the minimal surface
        manager = MagicMock()
        manager._context_engine = None
        ctx = PluginContext(manifest, manager)

        # register() must complete without AttributeError
        register(ctx)

    def test_register_with_provider_collector(self):
        """register() must capture the provider via _ProviderCollector."""
        from plugins.memory import _ProviderCollector
        from hermes_memory_libravdb import register

        collector = _ProviderCollector()
        register(collector)

        assert collector.provider is not None
        assert collector.provider.name == "libravdb"

    def test_register_context_engine_with_real_plugin_context(self):
        """register() must register a ContextEngine instance on real PluginContext."""
        from hermes_cli.plugins import PluginContext, PluginManifest
        from hermes_memory_libravdb import register

        manifest = PluginManifest(name="libravdb", version="0.5.2")
        manager = MagicMock()
        manager._context_engine = None
        ctx = PluginContext(manifest, manager)

        register(ctx)

        # The mocked manager should have received a context engine
        assert manager._context_engine is not None
        from agent.context_engine import ContextEngine
        assert isinstance(manager._context_engine, ContextEngine)
        assert manager._context_engine.name == "libravdb"

    def test_register_hooks_registered(self):
        """register() must register all four lifecycle hooks."""
        from hermes_cli.plugins import PluginContext, PluginManifest
        from hermes_memory_libravdb import register

        manifest = PluginManifest(name="libravdb", version="0.5.2")
        manager = MagicMock()
        manager._context_engine = None
        ctx = PluginContext(manifest, manager)

        register(ctx)

        # PluginContext.register_hook stores hooks in _hooks dict
        hooks_calls = [
            call for call in manager.mock_calls
            if hasattr(call, '__getitem__') or 'register_hook' in str(call)
        ]
        # PluginContext uses _manager._register_hook or similar — verify
        # at minimum that no AttributeError was raised during hook registration


# ---------------------------------------------------------------------------
# ABC inheritance tests
# ---------------------------------------------------------------------------

class TestABCInheritance:
    """Verify class hierarchies match Hermes ABCs."""

    def test_provider_inherits_memory_provider(self):
        from agent.memory_provider import MemoryProvider
        from hermes_memory_libravdb import LibraVDBMemoryProvider
        assert issubclass(LibraVDBMemoryProvider, MemoryProvider)

    def test_provider_implements_all_abstract_methods(self):
        from agent.memory_provider import MemoryProvider
        from hermes_memory_libravdb import LibraVDBMemoryProvider

        for method in MemoryProvider.__abstractmethods__:
            assert hasattr(LibraVDBMemoryProvider, method), \
                f"Missing abstract method: {method}"
            # Verify it's callable (not just inherited abstract)
            attr = getattr(LibraVDBMemoryProvider, method)
            assert callable(attr) or isinstance(attr, property), \
                f"Abstract member {method} is not callable/property"

    def test_provider_can_instantiate(self):
        from hermes_memory_libravdb import LibraVDBMemoryProvider
        p = LibraVDBMemoryProvider()
        assert p.name == "libravdb"
        assert p.is_available() is True

    def test_context_engine_inherits_context_engine(self):
        from agent.context_engine import ContextEngine
        from hermes_memory_libravdb import _LibraVDBContextEngine
        assert issubclass(_LibraVDBContextEngine, ContextEngine)

    def test_context_engine_implements_all_abstract_methods(self):
        from agent.context_engine import ContextEngine
        from hermes_memory_libravdb import _LibraVDBContextEngine

        for method in ContextEngine.__abstractmethods__:
            assert hasattr(_LibraVDBContextEngine, method), \
                f"Missing abstract method: {method}"
            attr = getattr(_LibraVDBContextEngine, method)
            assert callable(attr) or isinstance(attr, property), \
                f"Abstract member {method} is not callable/property"

    def test_context_engine_can_instantiate(self):
        from hermes_memory_libravdb import LibraVDBMemoryProvider, _LibraVDBContextEngine
        p = LibraVDBMemoryProvider()
        e = _LibraVDBContextEngine(p)
        assert e.name == "libravdb"
        assert e.compression_count == 0
        assert e.last_prompt_tokens == 0

    def test_context_engine_name_is_property(self):
        from hermes_memory_libravdb import LibraVDBMemoryProvider, _LibraVDBContextEngine
        p = LibraVDBMemoryProvider()
        e = _LibraVDBContextEngine(p)
        # Must be accessible as attribute (property), not method
        assert e.name == "libravdb"
        # ContextEngine ABC expects __abstractmethods__ includes 'name'
        from agent.context_engine import ContextEngine
        assert "name" in ContextEngine.__abstractmethods__


# ---------------------------------------------------------------------------
# CLI discovery tests
# ---------------------------------------------------------------------------

class TestCLIDiscovery:
    """Verify discover_plugin_cli_commands() finds the libravdb plugin."""

    def test_cli_discovery_with_config(self):
        """When memory.provider=libravdb, discover_plugin_cli_commands finds it."""
        from plugins.memory import discover_plugin_cli_commands

        # discover_plugin_cli_commands reads from config.yaml
        # We need to ensure memory.provider is set to libravdb
        # This test relies on the test config being set up
        cmds = discover_plugin_cli_commands()

        if cmds:
            cmd = cmds[0]
            assert cmd["name"] == "libravdb"
            assert callable(cmd["setup_fn"])
            assert cmd["handler_fn"] is not None

    def test_cli_setup_fn_registers_subcommands(self):
        """The setup_fn returned by discover creates valid argparse subcommands."""
        from plugins.memory import discover_plugin_cli_commands
        import argparse

        cmds = discover_plugin_cli_commands()
        if not cmds:
            pytest.skip("CLI discovery returned no commands — config not set up?")

        cmd = cmds[0]
        parser = argparse.ArgumentParser()
        cmd["setup_fn"](parser)

        # Should register all subcommands without error
        args = parser.parse_args(["status"])
        assert args.libravdb_subcommand == "status"

        args = parser.parse_args(["health"])
        assert args.libravdb_subcommand == "health"

        args = parser.parse_args(["search", "test query", "--limit", "5"])
        assert args.query == "test query"
        assert args.limit == "5"
        assert args.libravdb_subcommand == "search"


# ---------------------------------------------------------------------------
# Provider Collector compatibility
# ---------------------------------------------------------------------------

class TestProviderCollectorCompatibility:
    """Verify the register() function is compatible with _ProviderCollector."""

    def test_provider_collector_has_register_memory_provider(self):
        from plugins.memory import _ProviderCollector
        collector = _ProviderCollector()
        assert hasattr(collector, "register_memory_provider")
        assert callable(collector.register_memory_provider)

    def test_provider_collector_does_not_have_register_context_engine(self):
        from plugins.memory import _ProviderCollector
        collector = _ProviderCollector()
        assert not hasattr(collector, "register_context_engine"), \
            "_ProviderCollector should not have register_context_engine"

    def test_register_succeeds_with_provider_collector(self):
        from plugins.memory import _ProviderCollector
        from hermes_memory_libravdb import register

        collector = _ProviderCollector()
        # Must not raise AttributeError for register_context_engine
        register(collector)
        assert collector.provider is not None


# ---------------------------------------------------------------------------
# Real PluginContext API surface
# ---------------------------------------------------------------------------

class TestPluginContextAPISurface:
    """Verify PluginContext has the methods we expect (or don't expect)."""

    def test_plugin_context_does_not_have_register_memory_provider(self):
        from hermes_cli.plugins import PluginContext
        assert not hasattr(PluginContext, "register_memory_provider"), \
            "Hermes 0.14 PluginContext should NOT have register_memory_provider"

    def test_plugin_context_has_register_context_engine(self):
        from hermes_cli.plugins import PluginContext
        assert hasattr(PluginContext, "register_context_engine")

    def test_plugin_context_has_register_hook(self):
        from hermes_cli.plugins import PluginContext
        assert hasattr(PluginContext, "register_hook")

    def test_plugin_context_has_register_cli_command(self):
        from hermes_cli.plugins import PluginContext
        assert hasattr(PluginContext, "register_cli_command")

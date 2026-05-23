import hermes_memory_libravdb
from hermes_memory_libravdb import LibraVDBMemoryProvider, register
from plugins.memory import _ProviderCollector


class TestRegisterEntrypoint:
    def test_register_entrypoint_resolves(self):
        assert hasattr(hermes_memory_libravdb, "register")
        assert callable(hermes_memory_libravdb.register)

    def test_libravdb_package_exports(self):
        assert hasattr(hermes_memory_libravdb, "LibraVDBMemoryProvider")
        assert hasattr(hermes_memory_libravdb, "_get_hermes_home")
        assert hasattr(hermes_memory_libravdb, "_resolve_endpoint")
        assert hasattr(hermes_memory_libravdb, "_load_secret")

    def test_libravdb_is_memory_provider(self):
        """LibraVDBMemoryProvider must inherit from MemoryProvider ABC."""
        from agent.memory_provider import MemoryProvider
        assert issubclass(LibraVDBMemoryProvider, MemoryProvider)

    def test_libravdb_is_context_engine(self):
        """_LibraVDBContextEngine must inherit from ContextEngine ABC."""
        from agent.context_engine import ContextEngine
        from hermes_memory_libravdb.__init__ import _LibraVDBContextEngine
        assert issubclass(_LibraVDBContextEngine, ContextEngine)

    def test_register_with_provider_collector(self):
        """register() must work with Hermes 0.14's _ProviderCollector."""
        collector = _ProviderCollector()
        old = hermes_memory_libravdb._provider_instance
        hermes_memory_libravdb._provider_instance = None
        try:
            register(collector)
            assert collector.provider is not None
            assert isinstance(collector.provider, LibraVDBMemoryProvider)
        finally:
            hermes_memory_libravdb._provider_instance = old

    def test_register_with_plugin_context(self):
        """register() must not crash with Hermes 0.14's PluginContext."""
        from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager

        manager = PluginManager()
        manifest = PluginManifest(
            name="libravdb",
            source="entrypoint",
            path="hermes_memory_libravdb",
            key="libravdb",
        )
        ctx = PluginContext(manifest, manager)

        old = hermes_memory_libravdb._provider_instance
        hermes_memory_libravdb._provider_instance = None
        try:
            register(ctx)
            # Should register CLI commands
            assert "libravdb" in manager._cli_commands
        finally:
            hermes_memory_libravdb._provider_instance = old

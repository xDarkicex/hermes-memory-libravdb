import json

import hermes_memory_libravdb
from hermes_memory_libravdb import install
from hermes_memory_libravdb import _LibraVDBContextEngine
from hermes_memory_libravdb.provider import LibraVDBMemoryProvider


class TestRegisterEntrypoint:
    def test_register_entrypoint_resolves(self):
        assert hasattr(hermes_memory_libravdb, "register")
        assert callable(hermes_memory_libravdb.register)

    def test_libravdb_package_exports(self):
        assert hasattr(hermes_memory_libravdb, "LibraVDBMemoryProvider")
        assert hasattr(hermes_memory_libravdb, "_get_hermes_home")
        assert hasattr(hermes_memory_libravdb, "_resolve_endpoint")
        assert hasattr(hermes_memory_libravdb, "_load_secret")

    def test_install_writes_plugin_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert install.install(force=True) is True

        plugin_yaml = tmp_path / "plugins" / "libravdb" / "plugin.yaml"
        assert plugin_yaml.exists()
        assert "name: libravdb" in plugin_yaml.read_text()
    def test_invalid_context_threshold_config_degrades(self, tmp_path, monkeypatch):
        (tmp_path / "libravdb.json").write_text(
            json.dumps({"compactionThresholdFraction": "not-a-float"})
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        provider = LibraVDBMemoryProvider()
        provider.initialize("session")
        engine = _LibraVDBContextEngine(provider)

        assert engine.threshold_tokens == 1600
        assert engine.threshold_percent == 0.8
        assert "Invalid LibraVDB context config" in provider.system_prompt_block()

import hermes_memory_libravdb
from hermes_memory_libravdb import install


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

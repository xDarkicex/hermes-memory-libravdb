import hermes_memory_libravdb


class TestRegisterEntrypoint:
    def test_register_entrypoint_resolves(self):
        assert hasattr(hermes_memory_libravdb, "register")
        assert callable(hermes_memory_libravdb.register)

    def test_libravdb_package_exports(self):
        assert hasattr(hermes_memory_libravdb, "LibraVDBMemoryProvider")
        assert hasattr(hermes_memory_libravdb, "_get_hermes_home")
        assert hasattr(hermes_memory_libravdb, "_resolve_endpoint")
        assert hasattr(hermes_memory_libravdb, "_load_secret")
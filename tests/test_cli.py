import argparse

from hermes_memory_libravdb import cli


class TestCLIRegistersSubcommands:
    def test_cli_registers_subcommands(self):
        """All subcommands must parse without error."""
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)

        args = parser.parse_args(["status", "--deep"])
        assert args.deep is True
        assert args.libravdb_subcommand == "status"

        args = parser.parse_args(["status", "--index"])
        assert args.index is True
        assert args.libravdb_subcommand == "status"

        args = parser.parse_args(["flush", "--user-id", "alice"])
        assert args.user_id == "alice"
        assert args.libravdb_subcommand == "flush"

        args = parser.parse_args(["flush", "--user-id", "alice", "--force"])
        assert args.force is True
        assert args.libravdb_subcommand == "flush"

        args = parser.parse_args(["export", "--user-id", "alice"])
        assert args.user_id == "alice"
        assert args.libravdb_subcommand == "export"

        args = parser.parse_args(["journal", "--session-id", "sess-abc", "--limit", "25"])
        assert args.session_id == "sess-abc"
        assert args.limit == "25"
        assert args.libravdb_subcommand == "journal"

        args = parser.parse_args(
            ["dream-promote", "--user-id", "alice", "--dream-file", "/tmp/dream.md"]
        )
        assert args.user_id == "alice"
        assert args.dream_file == "/tmp/dream.md"
        assert args.libravdb_subcommand == "dream-promote"

    def test_register_cli_exposes_status_health_search(self):
        """Existing subcommands (status, health, search) must still parse correctly."""
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)

        args = parser.parse_args(["status"])
        assert args.libravdb_subcommand == "status"

        args = parser.parse_args(["health"])
        assert args.libravdb_subcommand == "health"

        args = parser.parse_args(["search", "my query"])
        assert args.query == "my query"
        assert args.libravdb_subcommand == "search"


class TestCLIFlushConfirmation:
    def test_flush_aborts_on_eof_confirmation(self, monkeypatch, capsys):
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        args = parser.parse_args(["flush", "--user-id", "alice"])

        monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))

        def fail_create_channel():
            raise AssertionError("flush should abort before creating a channel")

        monkeypatch.setattr(cli, "_create_cli_channel", fail_create_channel)

        cli.libravdb_command(args)

        out = capsys.readouterr().out
        assert "Flush aborted: confirmation required" in out

    def test_flush_aborts_when_namespace_confirmation_does_not_match(self, monkeypatch, capsys):
        parser = argparse.ArgumentParser()
        cli.register_cli(parser)
        args = parser.parse_args(["flush", "--user-id", "alice"])

        monkeypatch.setattr("builtins.input", lambda _prompt: "bob")

        def fail_create_channel():
            raise AssertionError("flush should abort before creating a channel")

        monkeypatch.setattr(cli, "_create_cli_channel", fail_create_channel)

        cli.libravdb_command(args)

        out = capsys.readouterr().out
        assert "Flush aborted: namespace confirmation did not match" in out

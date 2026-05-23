import json

import pytest

from hermes_memory_libravdb.identity import resolve_identity
from hermes_memory_libravdb.scopes import user_collection


def test_auto_identity_is_valid_when_username_starts_with_digit(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "123user")
    monkeypatch.setattr("socket.gethostname", lambda: "host.local")

    identity = resolve_identity(hermes_home=tmp_path, no_auto_persist=True)

    assert identity.user_id.startswith("u-123user@host.local#")
    assert user_collection(identity.user_id).startswith("user:u-123user@host.local#")


def test_auto_identity_sanitizes_unsupported_hostname_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setattr("socket.gethostname", lambda: "bad host/name")

    identity = resolve_identity(hermes_home=tmp_path, no_auto_persist=True)

    assert " " not in identity.user_id
    assert "/" not in identity.user_id
    assert user_collection(identity.user_id).startswith("user:alice@bad-host-name#")


def test_invalid_config_user_id_is_rejected():
    with pytest.raises(ValueError, match="Invalid collection name"):
        resolve_identity(config={"userId": "not valid"})


def test_invalid_persisted_user_id_is_rejected(tmp_path):
    (tmp_path / "libravdb-identity.json").write_text(
        json.dumps({"userId": "123bad"})
    )

    with pytest.raises(ValueError, match="Invalid collection name"):
        resolve_identity(hermes_home=tmp_path)

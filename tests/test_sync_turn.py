import json
import time
from unittest.mock import MagicMock

from hermes_memory_libravdb.provider import _NonceState, _GrpcChannel, LibraVDBMemoryProvider


class TestHealthNonceExtraction:
    def test_health_nonce_extraction(self):
        """Health RPC response metadata must populate the nonce state for HMAC signing."""
        secret = "test-secret"

        mock_metadata = MagicMock()
        mock_metadata.get = MagicMock(return_value="challenge-nonce-abc123")

        mock_resp = MagicMock()
        mock_resp.initial_metadata = MagicMock(return_value=mock_metadata)

        channel = _GrpcChannel(endpoint="unix:/tmp/test.sock", secret=secret)

        assert channel._nonce_state.get_nonce() is None

        channel._update_nonce_from_response(mock_resp, method_name="Health")

        assert channel._nonce_state.get_nonce() == "challenge-nonce-abc123"
        assert channel._nonce_state.should_sign("SomeSignedMethod") is True

    def test_nonce_absent_on_health_no_error(self):
        """If Health response has no nonce header, no exception is raised (auth may be disabled)."""
        mock_metadata = MagicMock()
        mock_metadata.get = MagicMock(return_value=None)

        mock_resp = MagicMock()
        mock_resp.initial_metadata = MagicMock(return_value=mock_metadata)

        channel = _GrpcChannel(endpoint="unix:/tmp/test.sock", secret="test-secret")

        channel._update_nonce_from_response(mock_resp, method_name="Health")

        assert channel._nonce_state.get_nonce() is None

    def test_signed_methods_blocked_without_nonce(self):
        """should_sign returns False when nonce is None, even if secret is set."""
        nonce_state = _NonceState(secret="has-secret-but-no-nonce")
        assert nonce_state.should_sign("SomeMethod") is False

        nonce_state.update_nonce("valid-nonce")
        assert nonce_state.should_sign("SomeMethod") is True

        assert nonce_state.should_sign("Health") is False


class TestSyncTurnReturnsImmediately:
    def test_sync_turn_returns_immediately(self):
        """sync_turn() must return without waiting for the ingest RPC to complete."""
        provider = LibraVDBMemoryProvider()
        provider._channel = MagicMock()
        provider._writes_enabled = True
        provider._session_id = "test-session"
        provider._user_id = None

        def slow_ingest(*args, **kwargs):
            time.sleep(5.0)
            return {"ok": True}

        provider._channel._call = slow_ingest

        start = time.time()
        provider.sync_turn("hello", "hi there")
        elapsed = time.time() - start

        assert elapsed < 1.0, f"sync_turn blocked for {elapsed:.2f}s — it must be non-blocking"

    def test_sync_turn_does_not_raise_when_channel_none(self):
        """sync_turn must not raise if _channel is None (degraded startup)."""
        provider = LibraVDBMemoryProvider()
        provider._channel = None
        provider._writes_enabled = True

        provider.sync_turn("hello", "hi")

    def test_sync_turn_skipped_when_writes_disabled(self):
        """sync_turn must not call the channel when _writes_enabled is False."""
        provider = LibraVDBMemoryProvider()
        provider._channel = MagicMock()
        provider._writes_enabled = False

        provider.sync_turn("hello", "hi")

        provider._channel._call.assert_not_called()


class TestSaveConfig:
    def test_save_config_does_not_persist_runtime_auth_secret(self, tmp_path):
        provider = LibraVDBMemoryProvider()

        provider.save_config(
            {
                "endpoint": "auto",
                "userId": "alice",
                "LIBRAVDB_AUTH_SECRET": "super-secret",
                "LIBRAVDB_AUTH_SECRET_FILE": "/tmp/secret",
            },
            str(tmp_path),
        )

        saved = json.loads((tmp_path / "libravdb.json").read_text())
        assert saved == {"endpoint": "auto", "userId": "alice"}

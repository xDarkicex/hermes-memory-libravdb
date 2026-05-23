import time
from unittest.mock import MagicMock

import pytest

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


class TestTLSCredentialLoading:
    def test_missing_configured_tls_ca_fails_closed(self, tmp_path):
        channel = _GrpcChannel(
            endpoint="tcp:remote.example:443",
            secret=None,
            tls_ca_path=str(tmp_path / "missing-ca.pem"),
        )

        with pytest.raises(RuntimeError, match="grpcEndpointTlsCa"):
            channel._create_channel()

    def test_empty_configured_tls_client_key_fails_closed(self, tmp_path):
        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("")
        channel = _GrpcChannel(
            endpoint="tcp:remote.example:443",
            secret=None,
            tls_client_cert_path=str(cert),
            tls_client_key_path=str(key),
        )

        with pytest.raises(RuntimeError, match="grpcEndpointTlsClientKey"):
            channel._create_channel()


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

"""Tests for user card CRUD tools (get_user_card, update_user_card, list_user_cards).

Requires hermes-agent to be installed; skipped in CI.
"""

import json
from unittest.mock import MagicMock

import pytest

from libravdb.ipc.v1 import rpc_pb2 as pb

pytest.importorskip("hermes_cli.plugins", reason="hermes-agent not installed")
pytest.importorskip("agent.memory_provider", reason="hermes-agent not installed")


def _make_provider(channel=None) -> "LibraVDBMemoryProvider":
    """Build a fresh provider with a mocked channel for testing tool dispatch."""
    from hermes_memory_libravdb.provider import LibraVDBMemoryProvider

    prov = LibraVDBMemoryProvider()
    prov._session_id = "test-session"
    prov._session_key = "test-session"
    prov._channel = channel or MagicMock()
    return prov


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------


class TestUserCardToolSchemas:
    """get_tool_schemas must include all three user card tools."""

    def test_get_user_card_in_schemas(self):
        """Schema must include get_user_card with required user_id param."""
        prov = _make_provider()
        schemas = prov.get_tool_schemas()
        names = [s["name"] for s in schemas]
        assert "get_user_card" in names

        schema = next(s for s in schemas if s["name"] == "get_user_card")
        assert "user_id" in schema["parameters"]["required"]

    def test_update_user_card_in_schemas(self):
        """Schema must include update_user_card with user_id and card params."""
        prov = _make_provider()
        schemas = prov.get_tool_schemas()
        names = [s["name"] for s in schemas]
        assert "update_user_card" in names

        schema = next(s for s in schemas if s["name"] == "update_user_card")
        required = schema["parameters"]["required"]
        assert "user_id" in required
        assert "card" in required

    def test_list_user_cards_in_schemas(self):
        """Schema must include list_user_cards with no required params."""
        prov = _make_provider()
        schemas = prov.get_tool_schemas()
        names = [s["name"] for s in schemas]
        assert "list_user_cards" in names

        schema = next(s for s in schemas if s["name"] == "list_user_cards")
        assert "required" not in schema["parameters"] or schema["parameters"]["required"] == []


# ---------------------------------------------------------------------------
# get_user_card
# ---------------------------------------------------------------------------


class TestGetUserCard:
    """handle_tool_call for get_user_card."""

    def test_returns_card_when_exists(self):
        """get_user_card must return card data when card exists."""
        prov = _make_provider()
        expected_card = json.dumps({"card": "A test user", "updatedAt": 1000})
        mock_resp = MagicMock(spec=pb.GetUserCardResponse)
        mock_resp.card_json = expected_card
        mock_resp.updated_at = 1000
        mock_resp.version = 1
        prov._channel._call = MagicMock(return_value=mock_resp)

        result = json.loads(prov.handle_tool_call("get_user_card", {"user_id": "test-user"}))
        assert result["card"] == expected_card
        assert result["updatedAt"] == 1000
        assert result["version"] == 1

    def test_returns_null_when_no_card(self):
        """get_user_card must return null card data when no card exists."""
        prov = _make_provider()
        mock_resp = MagicMock(spec=pb.GetUserCardResponse)
        mock_resp.card_json = ""
        prov._channel._call = MagicMock(return_value=mock_resp)

        result = json.loads(prov.handle_tool_call("get_user_card", {"user_id": "test-user"}))
        assert result["card"] is None or result["card"] == ""

    def test_requires_user_id(self):
        """get_user_card must error when user_id is missing."""
        prov = _make_provider()
        result = json.loads(prov.handle_tool_call("get_user_card", {}))
        assert "error" in result

    def test_returns_error_on_channel_failure(self):
        """get_user_card must return error when gRPC call fails."""
        prov = _make_provider()
        prov._channel._call = MagicMock(side_effect=Exception("daemon unreachable"))
        result = json.loads(
            prov.handle_tool_call("get_user_card", {"user_id": "test-user"})
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# update_user_card
# ---------------------------------------------------------------------------


class TestUpdateUserCard:
    """handle_tool_call for update_user_card."""

    def test_updates_card_successfully(self):
        """update_user_card must return ok when card update succeeds."""
        prov = _make_provider()
        mock_resp = MagicMock(spec=pb.UpsertUserCardResponse)
        mock_resp.ok = True
        prov._channel._call = MagicMock(return_value=mock_resp)

        result = json.loads(
            prov.handle_tool_call(
                "update_user_card",
                {"user_id": "test-user", "card": "A friendly test user."},
            )
        )
        assert result["ok"] is True

    def test_requires_user_id(self):
        """update_user_card must error when user_id is missing."""
        prov = _make_provider()
        result = json.loads(
            prov.handle_tool_call("update_user_card", {"card": "some text"})
        )
        assert "error" in result

    def test_requires_card(self):
        """update_user_card must error when card text is missing."""
        prov = _make_provider()
        result = json.loads(
            prov.handle_tool_call("update_user_card", {"user_id": "test-user"})
        )
        assert "error" in result

    def test_sends_upsert_user_card_rpc(self):
        """update_user_card must call UpsertUserCard RPC with correct params."""
        prov = _make_provider()
        mock_resp = MagicMock(spec=pb.UpsertUserCardResponse)
        mock_resp.ok = True
        prov._channel._call = MagicMock(return_value=mock_resp)

        prov.handle_tool_call(
            "update_user_card",
            {"user_id": "test-user", "card": "A friendly test user."},
        )

        prov._channel._call.assert_called_once()
        args = prov._channel._call.call_args[0]
        method_name = args[0]
        req = args[1]

        assert method_name == "UpsertUserCard"
        assert req.user_id == "test-user"
        # card_json must be valid JSON wrapping the card text
        body = json.loads(req.card_json)
        assert body["card"] == "A friendly test user."


# ---------------------------------------------------------------------------
# list_user_cards
# ---------------------------------------------------------------------------


class TestListUserCards:
    """handle_tool_call for list_user_cards."""

    def test_lists_cards_when_exist(self):
        """list_user_cards must return user list when cards exist."""
        prov = _make_provider()
        # Simulate a ListByMetaResponse with one result
        meta = json.dumps({
            "_user_id": "user-1",
            "card_json": json.dumps({"card": "User one"}),
            "updated_at": 1000,
            "version": 1,
        }).encode("utf-8")
        result_entry = MagicMock()
        result_entry.id = "card-1"
        result_entry.score = 1.0
        result_entry.text = "User one"
        result_entry.metadataJson = meta

        mock_resp = MagicMock()
        mock_resp.results = [result_entry]
        prov._channel._call = MagicMock(return_value=mock_resp)

        result = json.loads(prov.handle_tool_call("list_user_cards", {}))
        assert result["total"] == 1
        assert result["users"][0]["user_id"] == "user-1"

    def test_returns_empty_when_no_cards(self):
        """list_user_cards must return empty list when no cards exist."""
        prov = _make_provider()
        mock_resp = MagicMock()
        mock_resp.results = []
        prov._channel._call = MagicMock(return_value=mock_resp)

        result = json.loads(prov.handle_tool_call("list_user_cards", {}))
        assert result["total"] == 0
        assert result["users"] == []

    def test_uses_list_by_meta_with_correct_query(self):
        """list_user_cards must call ListByMeta with type=user_card."""
        prov = _make_provider()
        mock_resp = MagicMock()
        mock_resp.results = []
        prov._channel._call = MagicMock(return_value=mock_resp)

        prov.handle_tool_call("list_user_cards", {})

        prov._channel._call.assert_called_once()
        args = prov._channel._call.call_args[0]
        assert args[0] == "ListByMeta"
        req = args[1]
        assert req.key == "type"
        assert req.value == "user_card"
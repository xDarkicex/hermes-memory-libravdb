"""Tests for profile-scoped collection names and search scope resolution."""

import os
import pytest
from hermes_memory_libravdb.scopes import (
    profile_collection,
    PROFILE_COLLECTION_PREFIX,
    resolve_search_scopes,
    resolve_exact_recall_collections,
    GLOBAL_COLLECTION,
    USER_COLLECTION_PREFIX,
)


class TestProfileCollection:
    def test_profile_collection_returns_prefixed_name(self):
        result = profile_collection("drey")
        assert result == "profile:drey"

    def test_profile_collection_rejects_empty(self):
        with pytest.raises(ValueError, match="profile_name must be non-empty"):
            profile_collection("")

    def test_profile_collection_strips_whitespace(self):
        result = profile_collection("  vex  ")
        assert result == "profile:vex"

    def test_profile_collection_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="Invalid collection name"):
            profile_collection("bad name")


class TestResolveSearchScopesWithProfile:
    def test_profile_collection_inserted_between_session_and_user(self):
        collections = resolve_search_scopes(
            user_id="jeremy",
            session_id="abc123",
            profile_name="drey",
            cross_session_recall=True,
        )
        assert collections == [
            "session:abc123",
            "profile:drey",
            "user:jeremy",
            GLOBAL_COLLECTION,
        ]

    def test_no_profile_when_not_configured(self):
        collections = resolve_search_scopes(
            user_id="jeremy",
            session_id="abc123",
            profile_name=None,
            cross_session_recall=True,
        )
        assert "profile:" not in " ".join(collections)

    def test_profile_without_cross_session_omits_profile_and_user(self):
        collections = resolve_search_scopes(
            user_id="jeremy",
            session_id="abc123",
            profile_name="drey",
            cross_session_recall=False,
        )
        assert collections == ["session:abc123"]

    def test_profile_without_session_still_includes_profile(self):
        collections = resolve_search_scopes(
            user_id="jeremy",
            session_id=None,
            profile_name="vex",
            cross_session_recall=True,
        )
        assert collections == [
            "profile:vex",
            "user:jeremy",
            GLOBAL_COLLECTION,
        ]


class TestExactRecallWithProfile:
    def test_profile_in_exact_recall(self):
        collections = resolve_exact_recall_collections(
            user_id="jeremy",
            profile_name="drey",
            cross_session_recall=True,
        )
        assert collections == [
            "profile:drey",
            "user:jeremy",
            GLOBAL_COLLECTION,
        ]

    def test_exact_recall_without_profile(self):
        collections = resolve_exact_recall_collections(
            user_id="jeremy",
            profile_name=None,
            cross_session_recall=True,
        )
        assert collections == [
            "user:jeremy",
            GLOBAL_COLLECTION,
        ]

    def test_exact_recall_without_cross_session(self):
        collections = resolve_exact_recall_collections(
            user_id="jeremy",
            profile_name="drey",
            cross_session_recall=False,
        )
        assert collections == []

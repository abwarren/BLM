"""AUTHENTICATION — SEEDING & MIGRATION (§11, §10).

One named test per requirement:

    running the seed twice does NOT create duplicate users
    the resulting users are  admin role=admin  /  <user> role=user
    credentials are supplied via the environment, not tracked source
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.auth.seed import main as seed_main
from blm_v4.auth.seed import seed
from blm_v4.auth.store import AuthStore


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    admin_pw = "seed-admin-" + os.urandom(6).hex()
    user_pw = "seed-user-" + os.urandom(6).hex()
    monkeypatch.setenv("BLM_SEED_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BLM_SEED_ADMIN_PASSWORD", admin_pw)
    monkeypatch.setenv("BLM_SEED_USER_USERNAME", "bradblm")
    monkeypatch.setenv("BLM_SEED_USER_PASSWORD", user_pw)
    monkeypatch.setenv("BLM_AUTH_DB", str(tmp_path / "blm_auth.db"))
    monkeypatch.setenv("BLM_AUTH_BCRYPT_ROUNDS", "4")
    monkeypatch.delenv("BLM_SEED_RESET_PASSWORDS", raising=False)
    return {"db": str(tmp_path / "blm_auth.db"), "admin_pw": admin_pw,
            "user_pw": user_pw}


def _run_stdout(store, **kw):
    buf = io.StringIO()
    report = seed(store, rounds=4, out=buf, **kw)
    return report, buf.getvalue()


def test_seed_creates_exactly_the_two_expected_users(seeded):
    store = AuthStore(seeded["db"])
    report, out = _run_stdout(store)
    assert sorted(u for u, _ in report["created"]) == ["admin", "bradblm"]
    assert store.count_users() == 2
    assert store.get_user_by_username("admin")["role"] == "admin"
    assert store.get_user_by_username("bradblm")["role"] == "user"
    assert "admin" in out and "bradblm" in out


def test_running_the_seed_twice_creates_no_duplicate_users(seeded):
    store = AuthStore(seeded["db"])
    first, _ = _run_stdout(store)
    assert len(first["created"]) == 2
    second, out = _run_stdout(store)
    assert second["created"] == []
    assert len(second["existing"]) == 2
    assert store.count_users() == 2
    assert "unchanged" in out


def test_seed_is_idempotent_across_process_boundaries(seeded):
    """A second CLI invocation in the same environment must also be a no-op."""
    assert seed_main([]) == 0
    assert seed_main([]) == 0
    assert AuthStore(seeded["db"]).count_users() == 2


def test_seed_requires_environment_passwords_to_create_accounts(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("BLM_AUTH_DB", str(tmp_path / "blm_auth.db"))
    monkeypatch.delenv("BLM_SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BLM_SEED_USER_PASSWORD", raising=False)
    store = AuthStore(str(tmp_path / "blm_auth.db"))
    report, out = _run_stdout(store)
    assert store.count_users() == 0
    assert len(report["skipped"]) == 2
    assert "skipped" in out


def test_seed_never_prints_a_password(seeded):
    store = AuthStore(seeded["db"])
    _, out = _run_stdout(store)
    assert seeded["admin_pw"] not in out
    assert seeded["user_pw"] not in out
    assert "$2b$" not in out


def test_seed_does_not_overwrite_an_existing_password_by_default(seeded):
    from blm_v4.auth.passwords import hash_password, verify_password
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    # rotate the admin password OUT of band
    row = store.get_user_by_username("admin")
    store.set_password(row["id"], hash_password("operator-changed-it",
                                                rounds=4))
    _run_stdout(store)
    stored = store.get_user_by_username("admin")["password_hash"]
    assert verify_password("operator-changed-it", stored) is True
    assert verify_password(seeded["admin_pw"], stored) is False


def test_seed_reset_flag_rotates_the_password(seeded):
    from blm_v4.auth.passwords import verify_password
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    report, _ = _run_stdout(store, reset_passwords=True)
    assert len(report["reset"]) == 2
    stored = store.get_user_by_username("admin")["password_hash"]
    assert verify_password(seeded["admin_pw"], stored) is True


def test_seed_reactivates_a_disabled_account(seeded):
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    row = store.get_user_by_username("bradblm")
    store.set_active(row["id"], False)
    _run_stdout(store)
    assert store.get_user_by_username("bradblm")["is_active"] == 1


def test_seed_corrects_a_drifted_role(seeded):
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    row = store.get_user_by_username("bradblm")
    store.set_role(row["id"], "admin")
    _run_stdout(store)
    assert store.get_user_by_username("bradblm")["role"] == "user"


def test_seed_honours_custom_usernames_from_the_environment(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv("BLM_AUTH_DB", str(tmp_path / "blm_auth.db"))
    monkeypatch.setenv("BLM_AUTH_BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("BLM_SEED_ADMIN_USERNAME", "root-op")
    monkeypatch.setenv("BLM_SEED_ADMIN_PASSWORD", "pw-" + os.urandom(6).hex())
    monkeypatch.delenv("BLM_SEED_USER_PASSWORD", raising=False)
    store = AuthStore(str(tmp_path / "blm_auth.db"))
    report, _ = _run_stdout(store)
    assert [u for u, _ in report["created"]] == ["root-op"]
    assert store.get_user_by_username("root-op")["role"] == "admin"


def test_seeded_password_is_stored_only_as_a_hash(seeded):
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    row = store.get_user_by_username("admin")
    assert row["password_hash"].startswith("bcrypt_sha256$")
    raw = Path(seeded["db"]).read_bytes()
    assert seeded["admin_pw"].encode() not in raw
    assert seeded["user_pw"].encode() not in raw


def test_schema_creation_is_additive_and_survives_reopening(seeded):
    """Re-opening the store must not rebuild or lose rows."""
    store = AuthStore(seeded["db"])
    _run_stdout(store)
    reopened = AuthStore(seeded["db"])
    assert reopened.count_users() == 2
    # running the schema again is a no-op, not a reset
    assert reopened.list_users()[0]["username"] in ("admin", "bradblm")

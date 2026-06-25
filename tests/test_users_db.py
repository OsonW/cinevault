import pytest
import users_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh users.db in an isolated temp dir. users_db reads DB_DIR on every
    call, so setting the env var and calling init is enough — no reload needed."""
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    users_db.init_users_db()
    return users_db


def test_update_username_success(db):
    uid = db.create_user("alice", "secret123")
    assert db.update_username(uid, "alice2") is True
    assert db.get_user_by_id(uid)["username"] == "alice2"


def test_update_username_collision_is_case_insensitive(db):
    db.create_user("alice", "secret123")
    uid2 = db.create_user("bob", "secret123")
    # bob tries to take ALICE (different case) -> rejected, row unchanged.
    assert db.update_username(uid2, "ALICE") is False
    assert db.get_user_by_id(uid2)["username"] == "bob"


def test_update_username_own_case_change_allowed(db):
    uid = db.create_user("Bobby", "secret123")
    # Fixing your own capitalization only conflicts with your own row -> allowed.
    assert db.update_username(uid, "bobby") is True
    assert db.get_user_by_id(uid)["username"] == "bobby"


def test_update_password_changes_hash(db):
    uid = db.create_user("alice", "oldpass1")
    db.update_password(uid, "newpass2")
    assert db.verify_password("alice", "oldpass1") is None
    assert db.verify_password("alice", "newpass2") is not None


# ── Keyless-account reaper ───────────────────────────────────────────────────

def _backdate(db, uid, expr="datetime('now', '-2 hours')"):
    """Rewrite a user's created_at to simulate an older registration."""
    with db._get_users_conn() as conn:
        conn.execute(f"UPDATE users SET created_at = {expr} WHERE id = ?", (uid,))


def test_stale_keyless_user_is_deleted(db):
    uid = db.create_user("ghost", "secret123")     # never set a TMDB key
    _backdate(db, uid)                             # registered 2h ago
    assert db.delete_stale_keyless_users(3600) == [uid]
    assert db.get_user_by_id(uid) is None


def test_recent_keyless_user_is_kept(db):
    uid = db.create_user("fresh", "secret123")     # within the 1h lifespan
    assert db.delete_stale_keyless_users(3600) == []
    assert db.get_user_by_id(uid) is not None


def test_keyed_user_never_reaped(db):
    uid = db.create_user("active", "secret123")
    db.set_user_keys(uid, "tmdbkey", "")           # has a TMDB key
    _backdate(db, uid, "datetime('now', '-10 days')")
    assert db.delete_stale_keyless_users(3600) == []
    assert db.get_user_by_id(uid) is not None

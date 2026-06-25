# Profile Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the header's logout (and API-keys) buttons with a single profile button that opens a Profile modal for viewing/changing username & password, managing API keys, logging out, and deleting the account — with case-insensitive duplicate-username handling.

**Architecture:** Two new `@login_required` Flask routes (`/auth/username`, `/auth/password`) backed by two new `users_db` helpers that lean on the existing `UNIQUE COLLATE NOCASE` username column. The frontend swaps two header icon buttons for one profile button and adds a Profile modal that reuses existing CSS tokens and the existing key-update modal (via `openKeyUpdate()`) and delete flow (via `/auth/cancel-registration`).

**Tech Stack:** Python 3 / Flask / Flask-Login / SQLite (bcrypt), vanilla JS + server-rendered Jinja template (`templates/index.html`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-25-profile-modal-design.md`

---

## File Structure

- **`users_db.py`** (modify) — add `update_username()`, `update_password()`.
- **`auth.py`** (modify) — add `_validate_username()` / `_validate_password()` helpers, refactor `register()` to use them, add `change_username()` + `change_password()` routes.
- **`tests/test_users_db.py`** (create) — unit tests for the two new DB helpers.
- **`tests/test_auth.py`** (modify) — route tests for the two new endpoints.
- **`templates/index.html`** (modify) — header button swap, Profile modal HTML, Profile CSS, Profile JS; remove the old `#logoutConfirmBackdrop` modal and its JS.

---

## Task 1: users_db — update_username & update_password

**Files:**
- Modify: `users_db.py`
- Test: `tests/test_users_db.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_users_db.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_users_db.py -v`
Expected: FAIL — `AttributeError: module 'users_db' has no attribute 'update_username'`.

- [ ] **Step 3: Implement the helpers**

In `users_db.py`, add these two functions immediately after `delete_user()` (after line 63):

```python
def update_username(user_id: int, new_username: str) -> bool:
    """Rename a user. Returns True on success, False if the username is already
    taken. Uniqueness is case-insensitive (the column is UNIQUE COLLATE NOCASE),
    and SQLite only conflicts a row against *other* rows — so a user may change
    only the capitalization of their own name."""
    try:
        with _get_users_conn() as conn:
            conn.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (new_username, user_id),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def update_password(user_id: int, new_password: str) -> None:
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with _get_users_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (pw_hash, user_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_users_db.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add users_db.py tests/test_users_db.py
git commit -m "feat: add update_username/update_password to users_db"
```

---

## Task 2: auth.py — shared validation helpers + register refactor

**Files:**
- Modify: `auth.py:114-134` (the `register()` route) and add helpers above it.
- Test: `tests/test_auth.py` (existing register tests are the safety net).

- [ ] **Step 1: Add the validation helpers**

In `auth.py`, add these two functions just after `_as_str()` (after line 25):

```python
def _validate_username(username: str) -> str | None:
    """Returns an error message, or None if the username is valid."""
    if not username:
        return "Username required"
    if len(username) < 4 or len(username) > 32:
        return "Username must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(username):
        return "Username cannot contain spaces"
    return None


def _validate_password(password: str) -> str | None:
    """Returns an error message, or None if the password is valid."""
    if not password:
        return "Password required"
    if len(password) < 4 or len(password) > 32:
        return "Password must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(password):
        return "Password cannot contain spaces"
    return None
```

- [ ] **Step 2: Refactor `register()` to use them**

Replace the validation block in `register()` ([auth.py:120-129](../../../auth.py#L120-L129)) — the lines from `if not username or not password:` through the whitespace checks — with:

```python
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    err = _validate_username(username) or _validate_password(password)
    if err:
        return jsonify({"error": err}), 400
```

Leave the rest of `register()` (the `create_user` / `login_user` lines) unchanged.

- [ ] **Step 3: Run existing auth tests to verify no regression**

Run: `python -m pytest tests/test_auth.py -v`
Expected: PASS — all existing register/login tests still green (especially `test_register_missing_password`, `test_register_short_password`).

- [ ] **Step 4: Commit**

```bash
git add auth.py
git commit -m "refactor: extract username/password validation helpers"
```

---

## Task 3: /auth/username route

**Files:**
- Modify: `auth.py` (add route after `logout()`, ~line 166); update the `users_db` import at the top.
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`:

```python
# ── Change username ───────────────────────────────────────────────────────────

def test_change_username_success(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "alice2"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["username"] == "alice2"
    # New name works for login afterward.
    client.get("/auth/logout")
    assert client.post(
        "/auth/login", json={"username": "alice2", "password": "secret123"}
    ).status_code == 200


def test_change_username_duplicate_case_insensitive(client):
    _register(client, username="alice", password="secret123")
    client.get("/auth/logout")
    _register(client, username="bobby", password="secret123")
    resp = client.post("/auth/username", json={"username": "ALICE"})
    assert resp.status_code == 409
    assert "taken" in resp.get_json()["error"]


def test_change_username_own_case_change(client):
    _register(client, username="Bobby", password="secret123")
    resp = client.post("/auth/username", json={"username": "bobby"})
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "bobby"


def test_change_username_same_name_is_noop(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "unchanged"


def test_change_username_invalid(client):
    _register(client, username="alice", password="secret123")
    resp = client.post("/auth/username", json={"username": "ab"})
    assert resp.status_code == 400


def test_change_username_unauthenticated_redirects(client):
    resp = client.post("/auth/username", json={"username": "alice2"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auth.py -k change_username -v`
Expected: FAIL — 404 (route not registered) for the authenticated cases.

- [ ] **Step 3: Add the import**

In `auth.py`, update the `users_db` import block ([auth.py:9-12](../../../auth.py#L9-L12)) to include the new helper:

```python
from users_db import (
    get_user_by_id, create_user, verify_password,
    get_user_keys, set_user_keys, update_username,
)
```

- [ ] **Step 4: Implement the route**

In `auth.py`, add immediately after the `logout()` route (after line 166):

```python
@auth_bp.route("/auth/username", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=3600)   # 10/hour per IP
def change_username():
    data         = request.get_json(silent=True) or {}
    new_username = _as_str(data.get("username"))
    err = _validate_username(new_username)
    if err:
        return jsonify({"error": err}), 400
    # Exact same name is a friendly no-op. (A case-only change, e.g. Bob->bob,
    # is NOT exact-equal, so it falls through and is applied below.)
    if new_username == current_user.username:
        return jsonify({"status": "unchanged", "username": new_username})
    if not update_username(int(current_user.id), new_username):
        return jsonify({"error": "Username already taken"}), 409
    return jsonify({"status": "ok", "username": new_username})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth.py -k change_username -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "feat: add POST /auth/username route"
```

---

## Task 4: /auth/password route

**Files:**
- Modify: `auth.py` (add route after `change_username()`; update import).
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`:

```python
# ── Change password ───────────────────────────────────────────────────────────

def test_change_password_success(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "oldpass1", "new_password": "newpass2"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    # Old password rejected, new password accepted.
    client.get("/auth/logout")
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "oldpass1"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "newpass2"}
    ).status_code == 200


def test_change_password_wrong_current(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "WRONG", "new_password": "newpass2"},
    )
    assert resp.status_code == 401


def test_change_password_invalid_new(client):
    _register(client, username="alice", password="oldpass1")
    resp = client.post(
        "/auth/password",
        json={"current_password": "oldpass1", "new_password": "ab"},
    )
    assert resp.status_code == 400


def test_change_password_unauthenticated_redirects(client):
    resp = client.post(
        "/auth/password",
        json={"current_password": "x", "new_password": "yyyy"},
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_auth.py -k change_password -v`
Expected: FAIL — 404 for the authenticated cases.

- [ ] **Step 3: Add the import**

Update the `users_db` import in `auth.py` to also import `update_password`:

```python
from users_db import (
    get_user_by_id, create_user, verify_password,
    get_user_keys, set_user_keys, update_username, update_password,
)
```

- [ ] **Step 4: Implement the route**

In `auth.py`, add immediately after `change_username()`:

```python
@auth_bp.route("/auth/password", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=3600)   # 10/hour per IP
def change_password():
    data       = request.get_json(silent=True) or {}
    current_pw = _as_str(data.get("current_password"))
    new_pw     = _as_str(data.get("new_password"))
    if not verify_password(current_user.username, current_pw):
        return jsonify({"error": "Current password is incorrect"}), 401
    err = _validate_password(new_pw)
    if err:
        return jsonify({"error": err}), 400
    update_password(int(current_user.id), new_pw)
    return jsonify({"status": "ok"})
```

- [ ] **Step 5: Run the full auth suite to verify it passes**

Run: `python -m pytest tests/test_auth.py tests/test_users_db.py -v`
Expected: PASS (all green).

- [ ] **Step 6: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "feat: add POST /auth/password route"
```

---

## Task 5: Frontend — header profile button + Profile modal markup & CSS

> No JS test harness exists in this repo; frontend correctness is verified by running the app (Task 7). Make the edits exactly as written.

**Files:**
- Modify: `templates/index.html` — header (lines 1345-1360), CSS (after line 1269), modal markup (after the old logout modal block).

- [ ] **Step 1: Swap the header buttons for one profile button**

Replace the entire `.settings-cluster` block ([templates/index.html:1345-1360](../../../templates/index.html#L1345-L1360)) — both the API-keys `key-btn` and the logout `key-btn` — with:

```html
    <div class="settings-cluster">
      <button class="key-btn" onclick="openProfileModal()" aria-label="Profile">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4"/>
          <path d="M4 20c0-4 3.6-6 8-6s8 2 8 6"/>
        </svg>
      </button>
    </div>
```

- [ ] **Step 2: Add the Profile modal CSS**

In the `<style>` block, immediately after the `.logout-delete-input:focus` rule ([templates/index.html:1269](../../../templates/index.html#L1269)), add:

```css
/* ── Profile modal ─────────────────────────────── */
.profile-box { width: 22rem; }
.profile-head {
  display: flex; flex-direction: column; align-items: center;
  gap: 0.5rem; margin-bottom: 1.125rem;
}
.profile-avatar {
  width: 3.25rem; height: 3.25rem; border-radius: 50%;
  border: 1px solid var(--border-md); color: var(--muted);
  display: flex; align-items: center; justify-content: center;
}
.profile-username { font-size: 0.9375rem; font-weight: 600; color: var(--text); }
.profile-rows { display: flex; flex-direction: column; }
.profile-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.625rem 0; border-top: 1px solid var(--border);
}
.profile-row:first-child { border-top: none; }
.profile-row-main { display: flex; flex-direction: column; gap: 0.125rem; min-width: 0; }
.profile-row-label { font-size: 0.6875rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
.profile-row-value { font-size: 0.8125rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; }
.profile-row-action {
  background: none; border: 1px solid var(--border-md);
  border-radius: var(--radius); padding: 0.3125rem 0.75rem;
  color: var(--muted); font-size: 0.75rem; cursor: pointer;
  font-family: 'DM Sans', sans-serif; transition: all 0.12s; flex-shrink: 0;
}
.profile-row-action:hover { color: var(--text); border-color: var(--border-md); background: rgba(255,255,255,0.04); }
.profile-edit { padding: 0 0 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
.profile-edit.hidden { display: none; }
.profile-input {
  background: var(--surface); border: 1px solid var(--border-md);
  border-radius: var(--radius); padding: 0.4375rem 0.625rem;
  color: var(--text); font-size: 0.75rem; font-family: 'DM Sans', sans-serif; outline: none;
}
.profile-input:focus { border-color: var(--border-md); }
.profile-edit-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
.profile-save { background: var(--accent, #6c5ce7); }
.profile-actions { margin-top: 1rem; }
.profile-logout-btn {
  width: 100%; background: none; border: 1px solid var(--border-md);
  border-radius: var(--radius); padding: 0.5625rem 1rem;
  color: var(--text); font-size: 0.8125rem; cursor: pointer;
  font-family: 'DM Sans', sans-serif; font-weight: 500; transition: all 0.12s;
}
.profile-logout-btn:hover { background: rgba(255,255,255,0.05); border-color: var(--border); }
```

> Note: `.profile-save` uses the `.key-confirm-delete` base (red) overridden to the accent colour. If `--accent` is not defined in this project's `:root`, drop the `.profile-save` rule entirely so Save keeps the red `.key-confirm-delete` styling — verify in Task 7 and adjust.

- [ ] **Step 3: Add the Profile modal markup; remove the old logout modal**

Delete the entire old logout modal block ([templates/index.html:1509-1529](../../../templates/index.html#L1509-L1529)), the `<div ... id="logoutConfirmBackdrop"> … </div>` element, and replace it with:

```html
<!-- ── Profile Modal ───────────────────────────── -->
<div class="key-confirm-backdrop hidden" id="profileModalBackdrop" onclick="onProfileBackdropClick(event)">
  <div class="key-confirm-box profile-box">
    <div class="profile-head">
      <div class="profile-avatar">
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4"/>
          <path d="M4 20c0-4 3.6-6 8-6s8 2 8 6"/>
        </svg>
      </div>
      <div class="profile-username" id="profileUsernameDisplay"></div>
    </div>

    <div class="profile-rows">
      <div class="profile-row">
        <div class="profile-row-main">
          <span class="profile-row-label">Username</span>
          <span class="profile-row-value" id="profileUsernameValue"></span>
        </div>
        <button class="profile-row-action" id="profileUsernameEditBtn" onclick="startUsernameEdit()">Edit</button>
      </div>
      <div class="profile-edit hidden" id="profileUsernameEdit">
        <input type="text" id="profileUsernameInput" class="profile-input" maxlength="32" placeholder="New username" autocomplete="off" oninput="validateUsernameEdit()">
        <div class="profile-edit-actions">
          <button class="key-confirm-cancel" onclick="cancelUsernameEdit()">Cancel</button>
          <button class="key-confirm-delete profile-save" id="profileUsernameSave" onclick="saveUsername()" disabled>Save</button>
        </div>
      </div>

      <div class="profile-row">
        <div class="profile-row-main">
          <span class="profile-row-label">Password</span>
        </div>
        <button class="profile-row-action" id="profilePasswordBtn" onclick="startPasswordEdit()">Change</button>
      </div>
      <div class="profile-edit hidden" id="profilePasswordEdit">
        <input type="password" id="profilePwCurrent" class="profile-input" maxlength="32" placeholder="Current password" autocomplete="current-password" oninput="validatePasswordEdit()">
        <input type="password" id="profilePwNew" class="profile-input" maxlength="32" placeholder="New password" autocomplete="new-password" oninput="validatePasswordEdit()">
        <input type="password" id="profilePwConfirm" class="profile-input" maxlength="32" placeholder="Confirm new password" autocomplete="new-password" oninput="validatePasswordEdit()">
        <div class="profile-edit-actions">
          <button class="key-confirm-cancel" onclick="cancelPasswordEdit()">Cancel</button>
          <button class="key-confirm-delete profile-save" id="profilePasswordSave" onclick="savePassword()" disabled>Save</button>
        </div>
      </div>

      <div class="profile-row">
        <div class="profile-row-main">
          <span class="profile-row-label">API keys</span>
          <span class="profile-row-value" id="profileKeysStatus">—</span>
        </div>
        <button class="profile-row-action" onclick="manageKeysFromProfile()">Manage</button>
      </div>
    </div>

    <div class="profile-actions">
      <button class="profile-logout-btn" onclick="doLogout()">Log out</button>
    </div>

    <div class="logout-delete-footer">
      <button class="logout-delete-link" id="profileDeleteLink" onclick="showProfileDeleteSection()">Delete account</button>
    </div>
    <div class="logout-delete-section hidden" id="profileDeleteSection">
      <div class="logout-delete-label">Type username [<span style="color:var(--text)" id="profileDeleteUsernameHint"></span>] to confirm deletion.</div>
      <div class="logout-delete-row">
        <input type="text" id="profileDeleteUsernameInput" class="logout-delete-input" placeholder="Username" autocomplete="off" oninput="validateProfileDelete()">
        <button class="key-confirm-delete" id="profileDeleteConfirmBtn" onclick="doDeleteAccount()" disabled>Delete Permanently</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: profile button + profile modal markup and styles"
```

---

## Task 6: Frontend — Profile modal JS (and remove old logout JS)

**Files:**
- Modify: `templates/index.html` — the `<script>` block: `_currentUsername` declaration (line 1548), and the logout/delete JS region (lines 1705-1771).

- [ ] **Step 1: Make `_currentUsername` mutable**

The username can now change at runtime, so it can't be `const`. Change [templates/index.html:1548](../../../templates/index.html#L1548) from:

```js
const _currentUsername = document.getElementById('entryGreeting').dataset.username;
```

to:

```js
let _currentUsername = document.getElementById('entryGreeting').dataset.username;
```

- [ ] **Step 2: Replace the old logout/delete JS with the profile JS**

Replace the entire region from the `// Logout` comment through the end of `doDeleteAccount()` ([templates/index.html:1705-1771](../../../templates/index.html#L1705-L1771)) — i.e. `openLogoutConfirm`, `closeLogoutConfirm`, `doLogout`, `showLogoutDeleteSection`, `validateLogoutDelete`, `doDeleteAccount` — with:

```js
// ─────────────────────────────────────────────
// Profile modal
// ─────────────────────────────────────────────
function openProfileModal() {
  // Reset every editable section back to its collapsed default.
  cancelUsernameEdit();
  cancelPasswordEdit();
  document.getElementById('profileDeleteLink').style.display = '';
  document.getElementById('profileDeleteSection').classList.add('hidden');
  document.getElementById('profileDeleteUsernameInput').value = '';
  document.getElementById('profileDeleteConfirmBtn').disabled = true;

  document.getElementById('profileUsernameDisplay').textContent = _currentUsername;
  document.getElementById('profileUsernameValue').textContent   = _currentUsername;

  const status = document.getElementById('profileKeysStatus');
  status.textContent = _cachedKeys.has_keys ? 'TMDB set' : 'Not set';
  refreshKeys().then(keys => {
    if (document.getElementById('profileModalBackdrop').classList.contains('hidden')) return;
    status.textContent = keys.has_keys ? 'TMDB set' : 'Not set';
  });

  document.getElementById('profileModalBackdrop').classList.remove('hidden');
}

function closeProfileModal() {
  document.getElementById('profileModalBackdrop').classList.add('hidden');
}

function onProfileBackdropClick(e) {
  if (e.target === document.getElementById('profileModalBackdrop')) closeProfileModal();
}

// ── Username editing ──
function startUsernameEdit() {
  document.getElementById('profileUsernameEditBtn').style.display = 'none';
  const input = document.getElementById('profileUsernameInput');
  input.value = _currentUsername;
  document.getElementById('profileUsernameEdit').classList.remove('hidden');
  validateUsernameEdit();
  input.focus();
  input.select();
}
function cancelUsernameEdit() {
  document.getElementById('profileUsernameEdit').classList.add('hidden');
  document.getElementById('profileUsernameInput').value = '';
  document.getElementById('profileUsernameEditBtn').style.display = '';
}
function validateUsernameEdit() {
  const val = document.getElementById('profileUsernameInput').value.trim();
  // Enable once it's a valid length and actually different from the current name.
  const ok = val.length >= 4 && val.length <= 32 && !/\s/.test(val) && val !== _currentUsername;
  document.getElementById('profileUsernameSave').disabled = !ok;
}
async function saveUsername() {
  const btn = document.getElementById('profileUsernameSave');
  if (btn.disabled) return;
  const username = document.getElementById('profileUsernameInput').value.trim();
  btn.disabled = true;
  btn.textContent = 'Saving…';
  let ok = false, errMsg = 'Something went wrong — please try again.';
  try {
    const res = await fetch('/auth/username', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    ok = res.ok;
    if (!ok) {
      try {
        const body = await res.json();
        errMsg = res.status === 429
          ? 'Too many attempts — please wait a moment and try again.'
          : (body.error || errMsg);
      } catch {}
    } else {
      const body = await res.json();
      _currentUsername = body.username;
    }
  } catch { errMsg = 'Network error — please check your connection and try again.'; }

  btn.textContent = 'Save';
  if (ok) {
    // Reflect the new name everywhere it's shown, without a reload.
    document.getElementById('profileUsernameDisplay').textContent = _currentUsername;
    document.getElementById('profileUsernameValue').textContent   = _currentUsername;
    const greeting = document.getElementById('entryGreeting');
    greeting.dataset.username = _currentUsername;
    greeting.innerHTML = 'Hello, <strong></strong>';
    greeting.querySelector('strong').textContent = _currentUsername;
    cancelUsernameEdit();
    showToast('Username updated.');
  } else {
    validateUsernameEdit();
    showToast(errMsg, 'error');
  }
}

// ── Password editing ──
function startPasswordEdit() {
  document.getElementById('profilePasswordBtn').style.display = 'none';
  document.getElementById('profilePasswordEdit').classList.remove('hidden');
  document.getElementById('profilePwCurrent').focus();
}
function cancelPasswordEdit() {
  document.getElementById('profilePasswordEdit').classList.add('hidden');
  ['profilePwCurrent', 'profilePwNew', 'profilePwConfirm'].forEach(id =>
    document.getElementById(id).value = '');
  document.getElementById('profilePasswordSave').disabled = true;
  document.getElementById('profilePasswordBtn').style.display = '';
}
function validatePasswordEdit() {
  const cur = document.getElementById('profilePwCurrent').value;
  const nw  = document.getElementById('profilePwNew').value;
  const cf  = document.getElementById('profilePwConfirm').value;
  const ok = cur.length > 0 && nw.length >= 4 && nw.length <= 32 && !/\s/.test(nw) && nw === cf;
  document.getElementById('profilePasswordSave').disabled = !ok;
}
async function savePassword() {
  const btn = document.getElementById('profilePasswordSave');
  if (btn.disabled) return;
  const current_password = document.getElementById('profilePwCurrent').value;
  const new_password     = document.getElementById('profilePwNew').value;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  let ok = false, errMsg = 'Something went wrong — please try again.';
  try {
    const res = await fetch('/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password, new_password }),
    });
    ok = res.ok;
    if (!ok) {
      try {
        const body = await res.json();
        errMsg = res.status === 429
          ? 'Too many attempts — please wait a moment and try again.'
          : (body.error || errMsg);
      } catch {}
    }
  } catch { errMsg = 'Network error — please check your connection and try again.'; }

  btn.textContent = 'Save';
  if (ok) {
    cancelPasswordEdit();
    showToast('Password updated.');
  } else {
    validatePasswordEdit();
    showToast(errMsg, 'error');
  }
}

// ── API keys ──
function manageKeysFromProfile() {
  closeProfileModal();
  openKeyUpdate();
}

// ── Logout ──
// Abort in-flight fetches first so the navigation isn't queued behind a busy
// worker (PythonAnywhere free tier has a single worker).
function doLogout() {
  abortInFlightRequests();
  window.location.href = '/auth/logout';
}

// ── Delete account ──
function showProfileDeleteSection() {
  document.getElementById('profileDeleteLink').style.display = 'none';
  document.getElementById('profileDeleteUsernameHint').textContent = _currentUsername;
  document.getElementById('profileDeleteSection').classList.remove('hidden');
  document.getElementById('profileDeleteUsernameInput').focus();
}
function validateProfileDelete() {
  const val = document.getElementById('profileDeleteUsernameInput').value.trim();
  document.getElementById('profileDeleteConfirmBtn').disabled =
    val.toLowerCase() !== _currentUsername.toLowerCase();
}
async function doDeleteAccount() {
  const btn = document.getElementById('profileDeleteConfirmBtn');
  if (btn.disabled) return;
  const username = document.getElementById('profileDeleteUsernameInput').value.trim();
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  abortInFlightRequests();
  try {
    await fetch('/auth/cancel-registration', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
  } catch {}
  window.location.href = '/login';
}
```

- [ ] **Step 3: Check for dangling references**

Run: `python -m pytest tests/ -q` (sanity: backend still green), then grep the template for any leftover references to the removed IDs/functions:

Run: `grep -n "logoutConfirm\|openLogoutConfirm\|closeLogoutConfirm\|logoutDelete" templates/index.html`
Expected: **no matches** (every reference was removed/renamed). If any remain, fix them.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: profile modal JS (username/password change, keys, logout, delete)"
```

---

## Task 7: Manual verification (run the app)

**Files:** none (verification only).

- [ ] **Step 1: Start the app**

Run: `python app.py` (or the project's usual run command) and open the local URL. Log in with an existing account (or register one).

- [ ] **Step 2: Verify the header & modal**

- The header shows a single **person** icon button (no separate wrench/door buttons).
- Clicking it opens the Profile modal centered, styled consistently with the rest of the site (matches the existing confirm dialogs). The avatar, current username, and three rows (Username / Password / API keys) render correctly. Confirm the Save button colour looks right; if `--accent` was undefined, apply the `.profile-save` fallback note from Task 5 Step 2.
- Clicking the backdrop closes the modal; clicking inside does not.

- [ ] **Step 3: Verify username change + duplicate handling**

- Edit → enter a new valid name → Save → toast "Username updated."; the modal caption/value and the underlying greeting update without reload.
- Edit → enter the name of a **second existing account in different case** → Save → toast "Username already taken." (the case-insensitive collision path).
- Edit → change only the capitalization of your own name → Save → succeeds.
- Reload the page; confirm the new username persists and login still works with any case.

- [ ] **Step 4: Verify password change**

- Change → wrong current password → toast "Current password is incorrect."
- Change → correct current + matching new (≥4 chars) → toast "Password updated." Log out and log back in with the new password.

- [ ] **Step 5: Verify API keys, logout, delete**

- Manage → the existing key-update modal opens and still saves keys.
- Log out → returns to the login screen in one click (no confirmation).
- Delete account → type-username-to-confirm enables only on a case-insensitive match → deletes and returns to `/login` (use a throwaway account).

- [ ] **Step 6: Final commit (if any verification fixes were made)**

```bash
git add -A
git commit -m "fix: profile modal verification adjustments"
```

---

## Self-Review Notes

- **Spec coverage:** header swap (T5), profile modal w/ username/password/keys/logout/delete (T5/T6), `/auth/username` + `/auth/password` (T3/T4), `update_username`/`update_password` (T1), validation-helper refactor (T2), case-insensitive duplicate matrix (T1 + T3 tests), live in-place username update (T6), tests (T1/T3/T4) — all covered.
- **Type/name consistency:** `update_username`/`update_password` names, route paths, and element IDs are used identically across DB → routes → template. `doLogout()`, `openKeyUpdate()`, `_cachedKeys`, `refreshKeys()`, `abortInFlightRequests()`, `showToast()` are all pre-existing and reused as-is.
- **Removed symbols:** `openLogoutConfirm`, `closeLogoutConfirm`, `showLogoutDeleteSection`, `validateLogoutDelete`, `#logoutConfirmBackdrop` and the standalone API-keys/logout header buttons — verified gone in T6 Step 3.

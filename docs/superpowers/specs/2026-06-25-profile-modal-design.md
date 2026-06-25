# Profile Modal — Design

**Date:** 2026-06-25
**Status:** Approved (pending spec review)

## Goal

Replace the standalone logout button in the header with a single **profile
button** (default "person" SVG) that opens a **Profile modal**. The modal
surfaces account info and consolidates account actions:

- View current username
- Change username (case-insensitive uniqueness)
- Change password (requires current password)
- Manage API keys (reuses the existing key-update modal)
- Log out (one click, no confirmation)
- Delete account (type-username-to-confirm, relocated from the logout dialog)

The standalone API-keys (wrench) button and logout (door) button are both
removed from the header; the profile button is the single entry point.

## Non-goals

- No change to the mandatory first-run API-key entry flow (`showKeyModal('entry')`).
- No change to the registration/login routes' behavior (only a small internal
  refactor to share validation helpers).
- No DB migration — the existing schema already supports everything needed.

## Current state (reference)

- Header `.settings-cluster` ([templates/index.html:1345-1360](../../../templates/index.html#L1345-L1360))
  has two `.key-btn` buttons: API keys → `openKeyUpdate()`, logout →
  `openLogoutConfirm()`.
- The logout confirm modal (`#logoutConfirmBackdrop`,
  [templates/index.html:1509-1529](../../../templates/index.html#L1509-L1529))
  bundles a logout confirm **and** an inline "Delete account" → type-username
  flow that calls `POST /auth/cancel-registration`.
- Username uniqueness is enforced at the DB layer:
  `username TEXT UNIQUE NOT NULL COLLATE NOCASE` ([users_db.py:25](../../../users_db.py#L25)).
- There is **no** username-change or password-change endpoint today.

## UI

### Header
Both existing buttons in `.settings-cluster` are replaced by one profile button
using the `.key-btn` class (so it matches the surrounding controls) with a
standard person SVG (circle head + shoulder arc). `onclick="openProfileModal()"`.

### Profile modal
A centered card reusing the site's tokens (`--border`, `--muted`, `--text`,
`--red`, blurred backdrop) — visually between the small `.key-confirm-box` and
the large `.key-modal-box`.

```
┌──────────────────────────────────────┐
│              ( person )                │  avatar: default profile SVG, circular
│               bob                      │  current username, bold
├──────────────────────────────────────┤
│  Username     bob            [Edit]    │ → inline: [ new name ] [Save] [Cancel]
│  Password                [Change]      │ → inline: current / new / confirm + Save
│  API keys     TMDB set       [Manage]  │ → openKeyUpdate() (existing modal)
├──────────────────────────────────────┤
│            [   Log out   ]             │  primary button → doLogout()
│           Delete account               │  danger link → type-username confirm
└──────────────────────────────────────┘
```

Interaction details:

- **Username row** — shows current name + *Edit*. Edit swaps the row for an
  input (prefilled with the current name) plus Save/Cancel. Save calls
  `POST /auth/username`. Per decision, **no current password is required** for a
  username change — only the uniqueness/validation check. On success the modal
  updates the greeting, the `_currentUsername` JS var, the avatar caption, and
  the delete-confirm hint **in place** (no page reload).
- **Password row** — a single *Change password* button (no masked dots). It
  reveals three fields: current / new / confirm-new. Save is disabled until all
  three are filled and new === confirm. Save calls `POST /auth/password`.
- **API keys row** — shows a short status ("TMDB set" / "Not set", derived from
  the existing `refreshKeys()` cache) and a *Manage* button that opens the
  existing key-update modal via `openKeyUpdate()`. The rich two-pane key modal
  (with its "How do I get this?" help panels) is reused as-is, not duplicated.
- **Log out** — primary button; one click → `doLogout()` (clears in-flight
  requests, navigates to `/auth/logout`). No confirmation dialog.
- **Delete account** — danger link that reveals the existing type-your-username
  confirmation, calling `POST /auth/cancel-registration` (unchanged endpoint).
  This logic is moved out of the old logout dialog into the profile modal.

The old `#logoutConfirmBackdrop` modal is removed; its delete-account markup and
JS (`showLogoutDeleteSection`, `validateLogoutDelete`, `doDeleteAccount`) are
relocated/renamed under the profile modal.

## Backend

### users_db.py

```python
def update_username(user_id: int, new_username: str) -> bool:
    """Returns True on success, False if the username is taken (case-insensitive)."""
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

### auth.py

Extract shared validation so register and the new routes never diverge:

```python
def _validate_username(username: str) -> str | None:
    """Returns an error message, or None if valid."""
    if not username:
        return "Username required"
    if len(username) < 4 or len(username) > 32:
        return "Username must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(username):
        return "Username cannot contain spaces"
    return None

def _validate_password(password: str) -> str | None:
    if not password:
        return "Password required"
    if len(password) < 4 or len(password) > 32:
        return "Password must be 4–32 characters"
    if not _NO_WHITESPACE_RE.match(password):
        return "Password cannot contain spaces"
    return None
```

`register()` is refactored to call these (preserving its existing messages /
status codes).

New routes:

```python
@auth_bp.route("/auth/username", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=3600)   # 10/hour per IP
def change_username():
    data = request.get_json(silent=True) or {}
    new_username = _as_str(data.get("username"))
    err = _validate_username(new_username)
    if err:
        return jsonify({"error": err}), 400
    # Exact same name → friendly no-op (case-only change still proceeds below).
    if new_username == current_user.username:
        return jsonify({"status": "unchanged", "username": new_username})
    if not update_username(int(current_user.id), new_username):
        return jsonify({"error": "Username already taken"}), 409
    return jsonify({"status": "ok", "username": new_username})


@auth_bp.route("/auth/password", methods=["POST"])
@login_required
@rate_limit(max_requests=10, window_seconds=3600)   # 10/hour per IP
def change_password():
    data         = request.get_json(silent=True) or {}
    current_pw   = _as_str(data.get("current_password"))
    new_pw       = _as_str(data.get("new_password"))
    if not verify_password(current_user.username, current_pw):
        return jsonify({"error": "Current password is incorrect"}), 401
    err = _validate_password(new_pw)
    if err:
        return jsonify({"error": err}), 400
    update_password(int(current_user.id), new_pw)
    return jsonify({"status": "ok"})
```

Because `load_user` reads the DB on every request, a username change is
reflected immediately on the next request without re-login; the session stays
valid (same user id).

## Duplicate-name handling (case-insensitive)

The `UNIQUE ... COLLATE NOCASE` column makes all of this fall out for free:

| Scenario | Result |
|---|---|
| Change to a name another user holds, any case (`alice`→`Bob`/`BOB`) | `IntegrityError` → `409 "Username already taken"` |
| Fix your own capitalization (`Bob`→`bob`) | Allowed — SQLite UNIQUE conflicts only against *other* rows, so a row may take a case-variant of itself |
| Submit your exact current name (`Bob`→`Bob`) | No-op (`status: "unchanged"`, modal closes) |
| Login afterward with any case | Works — `WHERE username = ?` on the NOCASE column matches case-insensitively |

No migration required.

## Testing (tests/test_auth.py)

- `update_username` success; collision with another user's name (any case) → False.
- `update_username` own-case change (`Bob`→`bob`) → True.
- `update_password` changes the hash; old password no longer verifies, new one does.
- `POST /auth/username`: success (returns new name); duplicate → 409; invalid
  (too short / whitespace) → 400; exact-same name → `unchanged`; unauthenticated → 401/redirect.
- `POST /auth/password`: success; wrong current password → 401; invalid new → 400;
  login works with the new password afterward; unauthenticated → 401/redirect.

## Out of scope / YAGNI

- No email, avatar upload, or profile fields beyond username/password.
- No "log out everywhere"/session invalidation on password change.
- No password-strength meter beyond the existing 4–32 / no-whitespace rules.

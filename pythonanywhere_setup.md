# Deploying CineVault to PythonAnywhere (Free Tier)

PythonAnywhere handles HTTPS, process management, and static file serving
automatically. You do not need nginx, gunicorn, systemd, or certbot.

---

## 1. Open a Bash Console

Log in to pythonanywhere.com → **Consoles** → **New console: Bash**

---

## 2. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/cinema-tracker.git ~/cinevault
```

---

## 3. Create a Virtualenv and Install Dependencies

```bash
mkvirtualenv --python=python3.12 cinevault
pip install -r ~/cinevault/requirements.txt
```

> If `mkvirtualenv` is not found, run `source virtualenvwrapper.sh` first.

---

## 4. Create the Data Directory

All SQLite files (`users.db`, `movie_tracker_*.db`) live here.

```bash
mkdir -p ~/data
```

---

## 5. Configure the WSGI File

Edit `~/cinevault/pythonanywhere_wsgi.py` and set your real `SECRET_KEY`:

```bash
nano ~/cinevault/pythonanywhere_wsgi.py
```

Change the `SECRET_KEY` line to a random string. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output and paste it as the `SECRET_KEY` value.

---

## 6. Set Up the Web App in the Dashboard

1. Go to the **Web** tab → **Add a new web app**
2. Choose **Manual configuration** → **Python 3.12**
3. Under **Code**:
   - **Source code:** `/home/<yourusername>/cinevault`
   - **Working directory:** `/home/<yourusername>/cinevault`
4. Under **Virtualenv:**
   - Path: `/home/<yourusername>/.virtualenvs/cinevault`
5. Under **WSGI configuration file**, click the link to edit it.
   Replace the entire contents with:

   ```python
   import sys
   sys.path.insert(0, '/home/<yourusername>/cinevault')
   from pythonanywhere_wsgi import application
   ```

   Replace `<yourusername>` with your actual PythonAnywhere username.

---

## 7. Configure Static Files

In the **Web** tab under **Static files**, add one entry:

| URL       | Directory                                  |
|-----------|--------------------------------------------|
| `/static/`| `/home/<yourusername>/cinevault/static/`   |

This lets PythonAnywhere serve `favicon.svg` efficiently without going through Flask.

---

## 8. Reload the Web App

Click the green **Reload** button at the top of the Web tab.

Visit `https://<yourusername>.pythonanywhere.com` — you should see the CineVault login page.

---

## 9. Outbound API Access (Free Tier Allowlist)

PythonAnywhere free accounts restrict outbound HTTP to a specific allowlist.
CineVault calls several external APIs. Add the following domains under
**Account** → **Allowlist** if they are not already present:

| Service | Domain |
|---------|--------|
| TMDB | `api.themoviedb.org` |
| TMDB images | `image.tmdb.org` |
| OpenLibrary | `openlibrary.org` |
| OpenLibrary covers | `covers.openlibrary.org` |
| MangaDex | `api.mangadex.org` |
| MangaDex uploads | `uploads.mangadex.org` |

---

## 10. Updating the App

To deploy new code:

```bash
# In a PythonAnywhere Bash console
cd ~/cinevault
git pull
```

Then click **Reload** in the Web tab.

---

## Environment Variables Reference

Both are set inside `pythonanywhere_wsgi.py`:

| Variable | Default in WSGI file | Description |
|----------|----------------------|-------------|
| `DB_DIR` | `~/data` (resolved at runtime) | Directory for all SQLite files |
| `SECRET_KEY` | *(must be changed)* | Flask session signing key — keep this secret |

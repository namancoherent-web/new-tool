# Requirements — one-time setup on a new laptop

1. Extract the project ZIP to a folder you'll keep (e.g. Desktop).
2. Copy `.env.example` to `.env` and paste in the `DEEPSEEK_API_KEY` value
   (get this from whoever manages the DeepSeek account).
3. Double-click `SETUP.bat`.

That's it. `SETUP.bat` automatically installs everything the tool needs
(Git, Python, Node.js, Chromium, and every Python/Node package) and then
launches the tool. The first run can take several minutes — don't close
the window.

After the first run, use `start.bat` to open the tool (fast, skips the
install checks) and `update.bat` whenever a new version is released.

See `SETUP_FOR_USER.md` for the full walkthrough with screenshots-style
detail, including troubleshooting.

---

## If SETUP.bat can't run (manual fallback)

`SETUP.bat` needs Windows Package Manager (`winget`), which almost every
modern Windows 10/11 laptop already has. If it reports winget is missing,
install "App Installer" from the Microsoft Store, then run `SETUP.bat`
again.

If you'd rather install everything by hand instead:

- **Git** — https://git-scm.com/downloads (default options)
- **Python 3.11+** — https://www.python.org/downloads/ (tick **"Add
  python.exe to PATH"** on the first install screen)
- **Node.js (LTS)** — https://nodejs.org/ (pick "LTS", not "Current")
- **Chromium** — https://www.chromium.org/getting-involved/download-chromium/
  (must install to the default location)

Once all four are installed, `start.bat` will work directly without
needing `SETUP.bat` at all.

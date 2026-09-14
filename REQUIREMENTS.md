# Requirements — one-time setup on a new laptop

Install these four things once, in order. Everything else (Python packages,
Node packages) is installed automatically by `start.bat`.

## 1. Git
Needed so `update.bat` can pull new versions of the tool.
Download: https://git-scm.com/downloads
During install, keep all default options.

## 2. Python 3.11 or newer
Download: https://www.python.org/downloads/
**Important:** on the first install screen, check the box **"Add python.exe to PATH"**
before clicking Install.

## 3. Node.js (LTS version)
Needed to run the web interface.
Download: https://nodejs.org/ (pick the "LTS" button, not "Current")
Default install options are fine.

## 4. Chromium
Needed for the Google AI Mode company-discovery step (a real, visible
browser window opens during a search — this is expected).
Download and install Chromium (not regular Chrome) from:
https://www.chromium.org/getting-involved/download-chromium/
It must be installed at the default location:
`C:\Users\<you>\AppData\Local\Chromium\Application\chrome.exe`

## After installing all four

1. Get the project folder onto the laptop (ask whoever is distributing this
   tool for the folder, or clone it: `git clone https://github.com/namancoherent-web/new-tool.git`).
2. Inside the project folder, copy `.env.example` to `.env` and fill in the
   `DEEPSEEK_API_KEY` value (get this from whoever manages the DeepSeek
   account — it is a paid key and should not be shared publicly).
3. Ask whoever set this up for the `captcha-raptor` folder (it's shared
   separately, not through GitHub) and place it directly inside the project
   folder, next to `start.bat`. Without it, the tool still works, but may
   occasionally hit a CAPTCHA it can't solve automatically during discovery.
4. Double-click `start.bat`. The first run will take a few minutes while it
   installs everything else automatically — after that, starting the tool
   takes a few seconds.

## Getting updates later

Whenever a new version is available, double-click `update.bat`. This pulls
the latest code from GitHub and reinstalls anything that changed. It will
overwrite any local edits to the tool's own files, so don't hand-edit files
in this folder — your `.env` file (with your API key) is never touched by
an update.

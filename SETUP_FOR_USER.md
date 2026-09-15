# Getting started — Market Universe Finder

A step-by-step walkthrough for setting this up on your laptop for the first
time. This only needs to be done once. After that, use `start.bat` to open
the tool and `update.bat` whenever told there's a new version.

---

## Step 1 — Install four programs

Do these in order. Each one only needs a few clicks.

### Git
1. Go to https://git-scm.com/downloads and download the installer for
   Windows.
2. Run it. Click "Next" through every screen, keeping the default options.
3. Click "Install", then "Finish".

(You won't use Git directly today — it's what makes `update.bat` work
later when a new version is released, so install it now to save a step.)

### Python
1. Go to https://www.python.org/downloads/ and click the yellow "Download
   Python" button.
2. Run the installer. **Before clicking anything else, tick the checkbox
   at the bottom that says "Add python.exe to PATH".** This step is easy
   to miss and the tool won't work without it.
3. Click "Install Now" and wait for it to finish.

### Node.js
1. Go to https://nodejs.org/ and click the button labeled **LTS**
   (not "Current").
2. Run the installer. Click "Next" through every screen with the default
   options, then "Install".

### Chromium
1. Go to https://www.chromium.org/getting-involved/download-chromium/ and
   download the Windows build.
2. Install it normally. This is the browser the tool uses to search Google
   — during a search you'll see a real browser window open and search
   Google automatically. That's expected, not an error.

---

## Step 2 — Get the tool folder

You'll receive a ZIP file from whoever set this up for you.

1. Save the ZIP file somewhere you'll remember, e.g. your Desktop.
2. Right-click it and choose "Extract All...", then "Extract".
3. Open the extracted folder — you should see files like `start.bat`,
   `update.bat`, and `REQUIREMENTS.md` inside. This folder is your project
   folder — keep it where it is (don't move it after this point).

You'll only get a ZIP this one time. From now on, `update.bat` handles
getting new versions — see the "Getting updates" section below.

**If you're setting this up yourself instead of receiving a ZIP:**
download it directly from GitHub at
https://github.com/namancoherent-web/new-tool — click the green "Code"
button, then "Download ZIP", and follow steps 1–3 above.

---

## Step 3 — Add your API key

1. Inside the project folder, find the file named `.env.example`.
2. Make a copy of it, and rename the copy to exactly `.env` (no ".example"
   at the end — if Windows is hiding file extensions, name it `.env` and
   ignore any warning about changing the extension).
3. Open `.env` in Notepad.
4. Find the line that says `DEEPSEEK_API_KEY=` and paste your key right
   after the `=` sign, with no spaces. Whoever set this up for you will
   give you this key — treat it like a password, don't share it.
5. Save and close the file.

---

## Step 4 — First launch

1. Double-click `start.bat`.
2. The first time only, this will take a few minutes — you'll see a lot
   of text scroll by while it installs everything it needs. This is
   normal. Don't close the window.
3. When it's done, two new windows will open (leave both running) and your
   web browser will open automatically to the tool's sign-in page.
4. Enter your email and click sign in — no password needed.

You're in. Any time after this, just double-click `start.bat` again — it
will skip the install steps and open in a few seconds.

---

## Running a search

1. **Step 1 of the wizard** — type the market name (e.g. "Global Food Thin
   Wafers Market") and the geography (e.g. "Global", "India", "Europe").
2. **Step 2** — choose "Write it myself" for a short scope, or "Describe it
   in your own words" to paste a detailed brief (product types, what to
   include/exclude, etc.) — the more detail here, the better the results.
3. **Step 3** — review your input, then click **Start run**.
4. A real Chromium browser window will open by itself and search Google
   automatically — don't close it or click into it unless it's stuck.
   This can take anywhere from 2 to 10 minutes depending on the market.
5. When it's done, you'll see the company list on screen with buttons to
   download it as CSV, Excel, or Word.

---

## Getting updates

When told a new version is ready:
1. Close `start.bat`'s two windows if they're open.
2. Double-click `update.bat`.
3. Wait for it to say "Update complete."
4. Double-click `start.bat` as usual.

Your `.env` file (API key) is never touched by an update — everything else
in the folder gets replaced with the latest version, so don't save your own
files inside this folder.

---

## If something goes wrong

- **"Python is not installed or not on PATH"** — reinstall Python and make
  sure you tick "Add python.exe to PATH" this time.
- **A CAPTCHA appears in the browser window during a search** — just solve
  it by hand (click the images/checkbox as usual); the search will
  continue automatically afterward.
- **The tool seems stuck for a long time** — searches can genuinely take
  several minutes; if it's been over 15 minutes with no visible progress,
  close everything and try `start.bat` again.
- **Anything else** — contact whoever set this up for you with a
  screenshot of the error.

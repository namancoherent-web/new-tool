# Getting started — Market Universe Finder

A step-by-step walkthrough for setting this up on your laptop for the first
time. This only needs to be done once.

---

## Step 1 — Get the tool folder

You'll receive a ZIP file from whoever set this up for you.

1. Save the ZIP file somewhere you'll remember, e.g. your Desktop.
2. Right-click it and choose "Extract All...", then "Extract".
3. Open the extracted folder — you should see a file called `SETUP.bat`
   inside. This folder is your project folder — keep it where it is
   (don't move it after this point).

You'll only get a ZIP this one time. From now on, `update.bat` handles
getting new versions — see the "Getting updates" section below.

---

## Step 2 — Add your API key

1. Inside the project folder, find the file named `.env.example`.
2. Make a copy of it, and rename the copy to exactly `.env` (no ".example"
   at the end — if Windows is hiding file extensions, name it `.env` and
   ignore any warning about changing the extension).
3. Open `.env` in Notepad.
4. Find the line that says `DEEPSEEK_API_KEY=` and paste your key right
   after the `=` sign, with no spaces. Whoever set this up for you will
   give you this key — treat it like a password, don't share it.
5. Save and close the file.

(If you skip this step, that's fine too — `SETUP.bat` will pause and open
this file for you automatically the first time you run it.)

---

## Step 3 — Run SETUP.bat

1. Double-click `SETUP.bat`.
2. It will check your laptop for everything it needs (Git, Python,
   Node.js, Chromium) and silently install anything missing — you don't
   need to click through any installers yourself. **The first run can
   take several minutes** — don't close the window, even if it looks like
   nothing is happening for a while.
3. If you didn't already add your API key in Step 2, a Notepad window
   will open asking you to paste it in — do that, save, close Notepad,
   then run `SETUP.bat` again.
4. Once everything's installed, it automatically opens the tool — two new
   windows will appear (leave both running) and your web browser will
   open to the sign-in page.
5. Enter your email and click sign in — no password needed.

You're in. **From now on, just double-click `start.bat`** (not
`SETUP.bat`) — it skips the install checks and opens in a few seconds.
`SETUP.bat` only needs to be run once, or again if `start.bat` ever
complains something is missing.

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

- **"Windows Package Manager (winget) was not found"** — search "App
  Installer" in the Microsoft Store, install it, then run `SETUP.bat`
  again. (This only happens on very old Windows setups — most laptops
  already have this.)
- **A CAPTCHA appears in the browser window during a search** — just solve
  it by hand (click the images/checkbox as usual); the search will
  continue automatically afterward.
- **The tool seems stuck for a long time** — searches can genuinely take
  several minutes; if it's been over 15 minutes with no visible progress,
  close everything and try `start.bat` again.
- **Anything else** — contact whoever set this up for you with a
  screenshot of the error.

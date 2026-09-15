#!/usr/bin/env python3
"""
Google AI Mode Direct Scraper - FIXED HEADLESS MODE
Uses Google's AI Mode page directly for reliable AI responses.

Pipeline:
- Open Google AI Mode
- Ask a question
- Grab HTML of main content area (with fallbacks)
- Clean HTML into plain text
- Heuristically slice out ONLY the AI answer part
- Collapse to a single well-formed paragraph
- Detect tables in the AI HTML and:
    - keep them as markdown internally
    - pretty-print them as ASCII tables using tabulate in the terminal
"""

import threading
import time
import random
import json
import re
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

from bs4 import BeautifulSoup
from tabulate import tabulate

# ChromeDriverManager().install() does a filesystem + version-check pass
# (sometimes a network round-trip) every time it's called. With multiple
# discovery attempts launching browsers concurrently, calling this fresh
# per-instance meant every single attempt re-did that check, and
# webdriver-manager's own cache-file locking could serialize otherwise-
# parallel browser launches on it -- confirmed as a real slowdown: attempts
# that should run side-by-side were visibly opening one at a time. Resolved
# once per process and reused for every scraper instance after that.
_driver_path_lock = threading.Lock()
_cached_driver_path: str | None = None


def _resolve_driver_path() -> str:
    global _cached_driver_path
    if _cached_driver_path is not None:
        return _cached_driver_path
    with _driver_path_lock:
        if _cached_driver_path is None:
            _cached_driver_path = ChromeDriverManager().install()
    return _cached_driver_path


class GoogleAIModeScraper:
    """Direct Google AI Mode scraper using the AI Mode URL"""

    # Google AI Mode URL (goes directly to AI Mode interface)
    AI_MODE_URL = (
        "https://google.com/search?q=&sourceid=chrome&ie=UTF-8&udm=50&aep=48&cud=0&qsubts=1764494340788"
    )

    # Chromium binary (installed via winget: Hibbiki.Chromium) used instead of
    # regular Chrome, per explicit requirement.
    CHROMIUM_BINARY = str(Path.home() / "AppData" / "Local" / "Chromium" / "Application" / "chrome.exe")

    def __init__(self, headless=True, verbose=True, profile_dir=None, binary_location=None):
        self.headless = headless
        self.verbose = verbose
        # Persistent Chrome profile directory -- without this, Selenium
        # launches a fresh throwaway profile every run, so any extension you
        # install (e.g. a CAPTCHA solver) or login session would vanish the
        # next time. Pointing at a real folder on disk makes installed
        # extensions and logins persist across runs, same as your normal
        # browser profile.
        self.profile_dir = profile_dir or str(Path(__file__).resolve().parent / "chrome_profile")
        self.binary_location = binary_location or self.CHROMIUM_BINARY
        self.driver = None
        self.setup_driver()

    def log(self, message, level="INFO"):
        if self.verbose:
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {level}: {message}")

    def setup_driver(self):
        """Setup Chromium driver with ENHANCED stealth options for headless"""
        chrome_options = Options()

        if self.binary_location:
            chrome_options.binary_location = self.binary_location

        if self.headless:
            # NEW: Use newer headless mode which is harder to detect
            chrome_options.add_argument("--headless=new")
            # CRITICAL: These make headless look more like a real browser
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-software-rasterizer")
            chrome_options.add_argument("--disable-dev-shm-usage")

        # Persistent profile -- lets manually-installed extensions (e.g. a
        # CAPTCHA solver) and any logged-in Google session survive between runs.
        chrome_options.add_argument(f"--user-data-dir={self.profile_dir}")

        # Load the captcha-raptor unpacked extension directly -- avoids
        # relying on it having been manually installed into the profile via
        # chrome://extensions, and --load-extension works even in headless=new.
        captcha_ext_dir = (
            Path(__file__).resolve().parent / "captcha-raptor" / "extension"
        )
        if captcha_ext_dir.is_dir():
            chrome_options.add_argument(f"--load-extension={captcha_ext_dir}")

        # Enhanced stealth options
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--start-maximized")
        
        # Better user agent
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        # Language settings (important!)
        chrome_options.add_argument("--lang=en-US")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        
        chrome_options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.notifications": 2,
                "intl.accept_languages": "en-US,en",
                "profile.managed_default_content_settings.images": 1,
            },
        )

        try:
            service = Service(_resolve_driver_path())
            # Add service args for better headless performance
            service.log_path = "NUL" if self.headless else None
            
            self.driver = webdriver.Chrome(service=service, options=chrome_options)

            # ENHANCED stealth JavaScript - more properties to hide automation
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                        
                        // Fix chrome detection
                        window.chrome = {
                            runtime: {},
                        };
                        
                        // Permissions fix
                        const originalQuery = window.navigator.permissions.query;
                        window.navigator.permissions.query = (parameters) => (
                            parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                        );
                    """
                },
            )

            self.log("✓ Browser driver initialized successfully")
        except Exception as e:
            self.log(f"✗ Failed to setup driver: {e}", "ERROR")
            raise

    def human_delay(self, min_sec=1, max_sec=3):
        """Random human-like delay"""
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)

    def human_type(self, element, text):
        """Type text with human-like variation"""
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

    def ask_ai_mode(self, question, use_url_query=True):
        """Ask question directly in Google AI Mode.

        use_url_query=True (default, faster/more reliable): puts the question
        directly in the URL's q= parameter and navigates there in one shot,
        so the answer starts rendering on page load. This skips finding the
        input box, clicking, clearing, and typing character-by-character --
        the most fragile and slowest part of this flow, and the exact
        sequence that has hung waiting on WebDriverWait in testing.
        Falls back to the type-and-submit flow if the URL approach doesn't
        yield a response.
        """
        try:
            self.log(f"🤖 Asking AI Mode: '{question}'")

            if use_url_query:
                import urllib.parse
                url = (
                    "https://www.google.com/search?q="
                    + urllib.parse.quote(question)
                    + "&sourceid=chrome&ie=UTF-8&udm=50"
                )
                self.driver.get(url)
                self.human_delay(4, 6)
                self._handle_cookies()
                self._wait_for_generation_complete()

                ai_response_html = self._extract_ai_response()
                if ai_response_html:
                    full_text, answer_only, tables_md = self._clean_html_and_extract_answer(
                        ai_response_html, question
                    )
                    answer_final = answer_only or full_text
                    return {
                        "question": question,
                        "answer": answer_final,
                        "tables": tables_md,
                        "raw_html": ai_response_html,
                        "success": True,
                        "timestamp": datetime.now().isoformat(),
                        "format": "text",
                    }
                self.log("URL-query approach found no response, falling back to type-and-submit flow", "WARNING")

            # Navigate directly to AI Mode page
            self.driver.get(self.AI_MODE_URL)
            # INCREASED wait time for headless mode
            self.human_delay(4, 6)

            # Handle cookie consent
            self._handle_cookies()

            # EXTENDED wait before looking for input
            self.human_delay(2, 3)

            # Find the "Ask anything" input box
            self.log("Looking for AI Mode input box...")
            input_selectors = [
                "//textarea[contains(@placeholder, 'Ask anything')]",
                "//textarea[@name='q']",
                "//textarea[contains(@aria-label, 'Search')]",
                "//input[@name='q']",
                "//div[@role='combobox']//textarea",
                "//textarea",  # Broader fallback
            ]

            search_input = None
            for selector in input_selectors:
                try:
                    search_input = WebDriverWait(self.driver, 15).until(  # Increased timeout
                        EC.presence_of_element_located((By.XPATH, selector))
                    )
                    # Make sure element is actually visible and interactable
                    WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    self.log(
                        f"✓ Found input box with selector: {selector[:50]}..."
                    )
                    break
                except TimeoutException:
                    continue

            if not search_input:
                # Take screenshot for debugging (works in headless too!)
                if self.headless:
                    self.driver.save_screenshot("ai_mode_debug.png")
                    self.log("Screenshot saved to ai_mode_debug.png for debugging")

                # Save page for debugging
                with open("ai_mode_page.html", "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                return {
                    "question": question,
                    "answer": None,
                    "tables": [],
                    "raw_html": None,
                    "success": False,
                    "error": "Could not find AI Mode input box. Page saved to ai_mode_page.html",
                    "format": None,
                }

            # Scroll element into view (important for headless)
            self.driver.execute_script("arguments[0].scrollIntoView(true);", search_input)
            self.human_delay(0.5, 1)

            # Click to focus
            search_input.click()
            self.human_delay(0.3, 0.5)

            # Clear and type the question
            search_input.clear()
            self.human_delay(0.3, 0.5)
            self.human_type(search_input, question)
            self.human_delay(0.5, 1)

            # Submit the question
            self.log("Submitting question...")
            search_input.send_keys(Keys.RETURN)

            # Wait for the streamed answer to actually finish generating
            # rather than a fixed guess -- long/complex briefs can take
            # 20-40s+ to fully stream, and a fixed short delay captures a
            # half-finished (or still-echoing-the-prompt) page.
            self.log("Waiting for AI response...")
            self._wait_for_generation_complete()

            # Extract the AI response (HTML)
            ai_response_html = self._extract_ai_response()

            if ai_response_html:
                full_text, answer_only, tables_md = self._clean_html_and_extract_answer(
                    ai_response_html, question
                )

                # Base answer: answer-only paragraph or full text fallback
                answer_final = answer_only or full_text

                return {
                    "question": question,
                    "answer": answer_final,
                    "tables": tables_md,  # list of markdown tables
                    "raw_html": ai_response_html,
                    "success": True,
                    "timestamp": datetime.now().isoformat(),
                    "format": "text",
                }
            else:
                # Save page and screenshot for debugging
                if self.headless:
                    self.driver.save_screenshot("ai_mode_no_response.png")
                with open("ai_mode_page.html", "w", encoding="utf-8") as f:
                    f.write(self.driver.page_source)
                return {
                    "question": question,
                    "answer": None,
                    "tables": [],
                    "raw_html": None,
                    "success": False,
                    "error": "No AI response found. Page saved to ai_mode_page.html",
                    "format": None,
                }

        except Exception as e:
            self.log(f"✗ Error asking AI Mode: {e}", "ERROR")
            # Take screenshot on error
            try:
                if self.headless:
                    self.driver.save_screenshot("ai_mode_error.png")
            except:
                pass
            return {
                "question": question,
                "answer": None,
                "tables": [],
                "raw_html": None,
                "success": False,
                "error": str(e),
                "format": None,
            }

    def _handle_cookies(self):
        """Handle cookie consent popup"""
        try:
            cookie_selectors = [
                "//button[contains(., 'Accept all')]",
                "//button[contains(., 'I agree')]",
                "//button[@id='L2AGLb']",
                "//button[contains(text(), 'Reject all')]",
            ]

            for selector in cookie_selectors:
                try:
                    button = WebDriverWait(self.driver, 5).until(  # Increased timeout
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    button.click()
                    self.log("✓ Cookie consent handled")
                    self.human_delay(1, 2)
                    return
                except TimeoutException:
                    continue
        except Exception:
            pass

    def _wait_for_generation_complete(self, max_wait=150, poll_interval=3, stable_checks=6):
        """Poll the main content area's text length until it stops growing.

        AI Mode streams its answer in; for long/complex prompts (a detailed
        multi-paragraph brief) generation can take 20-40s+, far longer than
        a fixed delay. Snapshotting too early captures a half-streamed or
        even pre-streamed (just the echoed prompt, "Transcribing...") page,
        which then parses as zero company mentions even though the browser
        request "succeeded". This waits until the text length is unchanged
        across `stable_checks` consecutive polls (or max_wait elapses),
        which works regardless of the exact DOM structure Google uses.
        """
        self.log("Waiting for AI Mode response to finish generating...")
        last_len = -1
        stable_count = 0
        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                body = self.driver.find_element(By.TAG_NAME, "body")
                body_text = body.text
                text_len = len(body_text)
            except Exception:
                continue

            # "Transcribing..." (mic idle label, always present once the
            # input box has rendered) is NOT itself a signal -- but
            # "Thinking a little longer" is AI Mode's own still-generating
            # indicator, and its presence means the answer captured so far
            # is incomplete even if the visible text length is momentarily
            # stable (streaming can pause between chunks, e.g. during that
            # "thinking" phase before the real answer starts appearing).
            still_generating = "Thinking a little longer" in body_text or "Thinking..." in body_text

            if text_len == last_len and text_len > 0 and not still_generating:
                stable_count += 1
                if stable_count >= stable_checks:
                    self.log(
                        f"Response appears complete (stable at {text_len} chars after {elapsed:.0f}s)"
                    )
                    return
            else:
                stable_count = 0
            last_len = text_len

        self.log(
            f"Generation wait timed out after {max_wait}s (last length: {last_len} chars) -- proceeding anyway",
            "WARNING",
        )

    def _extract_ai_response(self):
        """Extract AI response from the page as HTML (preserve structure)"""
        self.log("Extracting AI response...")

        # Multiple selectors to try for AI responses
        response_selectors = [
            # AI Mode specific (guesses)
            "//div[contains(@class, 'ai-mode-response')]",
            "//div[@data-attrid='AIResponse']",
            "//div[contains(@class, 'generated-content')]",

            # SGE/AI Overview selectors (guesses)
            "//div[@data-attrid='SGEAnswer']",
            "//div[contains(@class, 'VjFXz')]",
            "//div[contains(@class, 'ai-overview')]",
            "//div[contains(@class, 'SPZz6b')]",

            # General content areas
            "//div[@id='rso']//div[contains(@class, 'g')]",
            "//div[contains(@class, 'kp-blk')]",

            # Fallback - main content area
            "//div[@id='search']",
            "//div[@id='main']",
        ]

        for selector in response_selectors:
            try:
                elements = self.driver.find_elements(By.XPATH, selector)
                for element in elements:
                    html = element.get_attribute("innerHTML") or ""
                    html = html.strip()
                    if html and len(html) > 50:  # Reasonable response length
                        self.log(
                            f"✓ AI response HTML found ({len(html)} chars) with selector: {selector}"
                        )
                        return html
            except Exception:
                continue

        # Try getting all visible HTML as last resort
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            html = body.get_attribute("innerHTML") or ""
            html = html.strip()
            if html and len(html) > 100:
                self.log(
                    f"⚠️  Using body HTML as fallback ({len(html)} chars)"
                )
                return html
        except Exception:
            pass

        self.log("✗ No AI response found", "WARNING")
        return None

    def _clean_html_and_extract_answer(self, html: str, question: str):
        """
        Convert HTML -> cleaned text, then heuristically extract only
        the AI answer block based on the question and known footer markers.
        Also extracts any tables in the HTML and converts them to Markdown.
        Returns (full_clean_text, answer_only_text or '', [tables_markdown]).
        """
        if not html:
            return "", "", []

        soup = BeautifulSoup(html, "html.parser")

        # Remove noise: scripts, styles, SVGs, noscript, etc.
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

        # Extract tables as Markdown before flattening everything
        tables_md = self._extract_tables_markdown(soup)

        text = soup.get_text(separator="\n")
        # Strip and drop empty lines
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]

        full_clean = "\n".join(lines)
        full_clean = re.sub(r"\n{3,}", "\n\n", full_clean).strip()

        # Now slice out only the part that looks like the AI answer.
        answer_only = self._extract_answer_from_lines(lines, question)

        return full_clean, answer_only, tables_md

    def _extract_answer_from_lines(self, lines, question: str) -> str:
        """
        Heuristically find the answer block:

        - Find line containing the question text.
        - Start after that, skipping trivial UI lines.
        - Stop at footer markers like 'AI can make mistakes', 'Your feedback helps Google improve', etc.
        - Finally, collapse everything into ONE clean paragraph.
        """
        if not lines:
            return ""

        q = question.strip().lower()
        start_idx = 0

        # Find the first line containing the question
        for i, line in enumerate(lines):
            if q and q in line.lower():
                start_idx = i
                break

        # Move forward a bit to skip UI noise like 'Thinking', 'Searching', etc.
        i = start_idx + 1
        skip_exact = {
            "thinking",
            "searching",  # Added this - it was cutting off your answer!
            "draft",
            "meet ai mode",
            "filters and topics",
            "ai mode",
            "all",
            "images",
            "videos",
            "news",
            "more",
            "shopping",
            "maps",
            "books",
            "flights",
            "finance",
            "start new search",
            "help me pack for my trip to kerala next week",
            "compare leather sofas vs fabric sofas",
            "how to identify if the pashmina shawl i am buying is genuine?",
        }

        # Skip known UI elements
        while i < len(lines) and lines[i].strip().lower() in skip_exact:
            i += 1

        # More comprehensive footer markers
        footer_markers = [
            "ai can make mistakes",
            "ai overview can make mistakes",
            "ai overviews can make mistakes",
            "your feedback helps google improve",
            "thank you for your feedback",
            "thank you",
            "share more feedback",
            "report a problem",
            "report legal issue",
            "10 sites",
            "search results",
            "dismiss",
            "my ad centre",
            "turn on your visual search history",
            "feedback",
            "learn more about these results",
            "about this result",
            "sources",
            "view all",
            "see more",
        ]

        collected = []
        
        # Collect lines until we hit a footer marker
        for j in range(i, len(lines)):
            low = lines[j].lower()
            
            # Stop if we hit a clear footer marker
            if any(marker in low for marker in footer_markers):
                break
            
            # Skip single-word navigation/UI elements
            if len(lines[j].split()) == 1 and lines[j].lower() in skip_exact:
                continue
                
            collected.append(lines[j])

        # Join lines and collapse to a single paragraph
        answer = "\n".join(collected).strip()
        
        # Collapse everything to single paragraph (remove all newlines, keep single spaces)
        answer_single_paragraph = re.sub(r"\s+", " ", answer).strip()
        
        return answer_single_paragraph

    def _clean_cell_text(self, text: str) -> str:
        """
        Clean up table cell text:
        - collapse whitespace
        - strip trailing source junk like 'TechTarget +7', 'IBM +4', 'Quora +4'
        """
        t = re.sub(r"\s+", " ", text).strip()
        # Remove trailing 'SourceName +N' patterns we saw in examples
        t = re.sub(
            r"\s+(IBM|Quora|TechTarget)\s*\+\d+$", "", t, flags=re.IGNORECASE
        )
        return t

    def _extract_tables_markdown(self, soup: BeautifulSoup):
        """
        Find all <table> elements in the soup and convert them to Markdown-style tables.
        Cleans header/cell text for readability.
        Returns a list of strings, each string is one markdown table.
        """
        tables_md = []
        tables = soup.find_all("table")

        for table in tables:
            rows = table.find_all("tr")
            parsed_rows = []

            for row in rows:
                cells = row.find_all(["th", "td"])
                cell_texts = [
                    self._clean_cell_text(c.get_text(" ", strip=True))
                    for c in cells
                ]
                # Skip fully empty rows
                if any(cell_texts):
                    parsed_rows.append(cell_texts)

            if not parsed_rows:
                continue

            # Determine header row
            header = parsed_rows[0]
            data_rows = parsed_rows[1:] if len(parsed_rows) > 1 else []

            # Normalize header: if first cell starts with "feature", make it just "Feature"
            if header and header[0].lower().startswith("feature"):
                header[0] = "Feature"

            # Build markdown
            max_cols = max(len(r) for r in parsed_rows)
            header = header + [""] * (max_cols - len(header))
            norm_data_rows = [
                r + [""] * (max_cols - len(r)) for r in data_rows
            ]

            md_lines = []
            # Header
            md_lines.append("| " + " | ".join(header) + " |")
            # Separator
            md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            # Data rows
            for r in norm_data_rows:
                md_lines.append("| " + " | ".join(r) + " |")

            tables_md.append("\n".join(md_lines))

        return tables_md

    def close(self):
        """Close browser driver"""
        if self.driver:
            self.driver.quit()
            self.log("Browser closed")


def print_banner():
    """Print application banner"""
    banner = """
╔═══════════════════════════════════════════════════════════╗
║   Google AI Mode Direct Scraper (Paragraph + Tables)      ║
║                          FIXED                            ║
║  - Paragraph answers                                      ║
║  - Markdown tables rendered as ASCII with tabulate        ║
║  - Headless mode now works properly!                      ║
╚═══════════════════════════════════════════════════════════╝
    """
    print(banner)


def print_markdown_table_as_ascii(md: str):
    """
    Take a markdown table string and print it as a nice ASCII table using tabulate.
    """
    lines = [l.strip() for l in md.splitlines() if l.strip()]
    if len(lines) < 2:
        return

    # First line: header row, second: separator, rest: data
    header_line = lines[0]
    data_lines = lines[2:]  # skip separator row

    headers = [h.strip() for h in header_line.strip("|").split("|")]
    rows = []
    for line in data_lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    print(tabulate(rows, headers=headers, tablefmt="grid"))


def print_result(result):
    """Pretty print AI response"""
    print("\n" + "=" * 60)
    print(f"Question: {result['question']}")
    print(f"Success: {'✓' if result['success'] else '✗'}")
    print(f"Format: {result.get('format')}")
    print("-" * 60)

    if result["success"] and result.get("answer"):
        print("\n🤖 AI Response (paragraph):")
        print("-" * 60)
        print(result["answer"])

        tables = result.get("tables") or []
        if tables:
            for idx, md_table in enumerate(tables, start=1):
                print(f"\n📊 Table {idx}:")
                print_markdown_table_as_ascii(md_table)

    elif not result["success"]:
        print(f"\n❌ Error: {result.get('error', 'Unknown error')}")
        print("\n💡 Tips:")
        print("   - Check ai_mode_page.html to see the actual page")
        print("   - Check ai_mode_debug.png screenshot if available")
        print("   - Try running in non-headless mode to debug")
        print("   - Make sure you're signed into Google")

    print("=" * 60 + "\n")


def save_to_file(results, filename="ai_responses.json"):
    """Save results to JSON file"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"✓ Results saved to {filename}")


def main():
    """Main terminal interface"""
    print_banner()

    # Configuration
    print("Configuration:")
    headless_input = input(
        "Run in headless mode? (y/n) [default: n]: "
    ).strip().lower()
    headless = headless_input == "y"

    print("\nInitializing AI Mode scraper...")
    scraper = None
    all_results = []

    try:
        scraper = GoogleAIModeScraper(headless=headless)

        print("\n✓ Scraper ready!")
        print("\n📝 Commands:")
        print("  - Type your question and press Enter")
        print("  - Type 'batch' to ask multiple questions")
        print("  - Type 'save' to save all responses to file")
        print("  - Type 'quit' to exit")
        print("\n💡 Tip: Ask detailed questions for better AI responses!\n")

        while True:
            question = input("❓ Ask AI: ").strip()

            if not question:
                continue

            if question.lower() == "quit":
                print("\n👋 Exiting...")
                break

            elif question.lower() == "save":
                if all_results:
                    filename = (
                        input("Filename [ai_responses.json]: ").strip()
                        or "ai_responses.json"
                    )
                    save_to_file(all_results, filename)
                else:
                    print("❌ No results to save yet!")
                continue

            elif question.lower() == "batch":
                print("\n📋 Batch Mode - Enter questions (empty line to finish):")
                questions = []
                while True:
                    q = input(f"  Question {len(questions)+1}: ").strip()
                    if not q:
                        break
                    questions.append(q)

                if questions:
                    print(f"\n🚀 Processing {len(questions)} questions...")
                    for i, q in enumerate(questions, 1):
                        print(f"\n[{i}/{len(questions)}] Processing: {q}")
                        result = scraper.ask_ai_mode(q)
                        all_results.append(result)
                        print_result(result)

                        if i < len(questions):
                            delay = random.uniform(10, 20)
                            print(
                                f"⏳ Waiting {delay:.1f}s before next question..."
                            )
                            time.sleep(delay)
                continue

            # Single question
            result = scraper.ask_ai_mode(question)
            all_results.append(result)
            print_result(result)

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
    finally:
        if scraper:
            scraper.close()

        if all_results:
            save_choice = (
                input("\n💾 Save results before exiting? (y/n): ")
                .strip()
                .lower()
            )
            if save_choice == "y":
                save_to_file(all_results)


if __name__ == "__main__":
    main()
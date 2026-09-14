from __future__ import annotations

import queue
import threading
import traceback

import streamlit as st

from config import CONFIG
from pipeline.universe_builder import run_universe_search

st.set_page_config(page_title="Market Universe Finder", page_icon="🌐", layout="centered")


def _init_state() -> None:
    defaults = {
        "run_thread": None,
        "progress_queue": None,
        "progress_log": [],
        "result": None,
        "error": None,
        "running": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _run_pipeline_in_thread(market: str, geography: str, category_prompt: str, brief: str, q: queue.Queue) -> None:
    def progress_cb(stage: str, detail: str) -> None:
        q.put(("progress", stage, detail))

    try:
        result = run_universe_search(market, geography, category_prompt, brief=brief, progress_cb=progress_cb)
        q.put(("done", result, None))
    except Exception as e:
        q.put(("error", str(e), traceback.format_exc()))


def _drain_queue() -> None:
    q = st.session_state.progress_queue
    if q is None:
        return
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item[0] == "progress":
            _, stage, detail = item
            st.session_state.progress_log.append((stage, detail))
        elif item[0] == "done":
            st.session_state.result = item[1]
            st.session_state.running = False
        elif item[0] == "error":
            st.session_state.error = item[1]
            st.session_state.running = False


def main() -> None:
    _init_state()

    st.title("🌐 Market Universe Finder")
    st.caption("Universe Mode — discovers, verifies, and classifies real companies for a market. No hallucinated results.")

    if CONFIG.google_ai_mode_enabled and CONFIG.google_ai_mode_only:
        st.info(
            "Discovery mode: **Google AI Mode only**. This opens a visible browser window during the run "
            "and can take 15-25+ minutes for a full discovery pass targeting 140+ companies. "
            "Do not close the browser window while it's running."
        )
    elif CONFIG.google_ai_mode_enabled:
        st.info("Discovery mode: multi-source (DuckDuckGo/Wikipedia/Wikidata) + Google AI Mode backfill.")
    else:
        st.info("Discovery mode: multi-source (DuckDuckGo/Wikipedia/Wikidata).")

    with st.form("universe_search_form"):
        market = st.text_input("Market name", placeholder="e.g. Global Food Thin Wafers Market")
        geography = st.text_input("Geography", value="Global", placeholder="e.g. Europe, Global, India")
        category_prompt = st.text_input(
            "Category / role to include",
            placeholder="e.g. Parent Companies, Manufacturers, or leave blank / 'all relevant players' for every role",
        )
        brief = st.text_area(
            "Detailed brief (optional)",
            height=200,
            placeholder=(
                "Optional: paste a detailed scope brief here (inclusion/exclusion rules, segmentation, "
                "independence rules). This is passed directly to the market-understanding and classification "
                "stages, so it's the most reliable way to enforce specific exclusions "
                "(e.g. 'exclude semiconductor wafer manufacturers')."
            ),
        )
        submitted = st.form_submit_button("Start run", disabled=st.session_state.running)

    if submitted and not st.session_state.running:
        if not market.strip():
            st.error("Market name is required.")
        else:
            st.session_state.progress_log = []
            st.session_state.result = None
            st.session_state.error = None
            st.session_state.running = True
            q: queue.Queue = queue.Queue()
            st.session_state.progress_queue = q
            thread = threading.Thread(
                target=_run_pipeline_in_thread,
                args=(market.strip(), geography.strip() or "Global", category_prompt.strip(), brief.strip(), q),
                daemon=True,
            )
            st.session_state.run_thread = thread
            thread.start()
            st.rerun()

    if st.session_state.running:
        _drain_queue()
        st.subheader("Progress")
        for stage, detail in st.session_state.progress_log[-20:]:
            st.text(f"[{stage}] {detail}")
        st.info("Running... this page refreshes automatically.")
        import time

        time.sleep(1.5)
        st.rerun()

    if st.session_state.error:
        st.error(f"Run failed: {st.session_state.error}")
        with st.expander("Full error details"):
            st.code(st.session_state.error)
        st.session_state.error = None

    if st.session_state.result:
        result = st.session_state.result
        st.success(f"Done — {len(result.companies)} companies found in {result.duration_seconds:.0f}s")

        col1, col2, col3 = st.columns(3)
        col1.metric("Candidates found", result.total_candidates_found)
        col2.metric("Passed verification", result.total_verified)
        col3.metric("Final companies", len(result.companies))

        st.subheader("Downloads")
        dl_cols = st.columns(3)
        labels = {"csv": "CSV", "xlsx": "Excel (XLSX)", "docx": "Word (DOCX)"}
        for i, (fmt, path) in enumerate(result.output_paths.items()):
            with open(path, "rb") as f:
                dl_cols[i % 3].download_button(
                    f"Download {labels.get(fmt, fmt.upper())}",
                    data=f.read(),
                    file_name=path.name,
                    key=f"dl_{fmt}",
                )

        if result.companies:
            st.subheader("Preview")
            preview_rows = [
                {
                    "Company": c.company_name,
                    "Website": c.website,
                    "HQ Country": c.hq_country,
                    "Category": c.category,
                    "Confidence": c.confidence,
                }
                for c in result.companies
            ]
            st.dataframe(preview_rows, use_container_width=True)


if __name__ == "__main__":
    main()

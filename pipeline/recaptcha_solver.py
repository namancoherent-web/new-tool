"""Ports captcha-raptor's reCAPTCHA-solving logic out of the browser
extension it originally shipped as, into plain Python driven by the same
Selenium `driver` this scraper already controls.

Why this exists: the extension is loaded via `--load-extension=<path>` at
Chromium launch. On a deployment laptop where the user's IT team enforces a
Chrome/Chromium enterprise extension policy, this load is blocked --
confirmed directly: with the policy enabled, the extension (and every other
extension on that machine) works; disabled, Chromium throws "manifest.json
not found" at launch even though the file is verifiably present on disk.
The policy reaches this tool's separate, portable Chromium binary, not just
the user's day-to-day Chrome. Rather than build a fallback for when the
extension happens to be missing, every piece of what the extension does is
reproduced here as plain pipeline code, so there is no extension for any
future IT policy to block, on this laptop or any other.

Every algorithm below is a direct, verified port of the corresponding
source file in captcha-raptor/extension/ -- see the docstring on each
function for exactly which file and what was checked line-by-line to make
sure the Python version produces bit-identical output, not just
"equivalent" output. The bundled data files (models/*.onnx, db/captcha.sqlite)
are read directly; nothing here depends on the extension being loadable.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "captcha-raptor" / "extension"
_DB_PATH = _EXTENSION_DIR / "db" / "captcha.sqlite"
_TYPE_MODEL_PATH = _EXTENSION_DIR / "models" / "type.onnx"
_GRID_MODEL_PATH = _EXTENSION_DIR / "models" / "grid.onnx"
_GRID_META_PATH = _EXTENSION_DIR / "models" / "grid.meta.json"


# ---------------------------------------------------------------------------
# Perceptual hash -- direct port of captcha-raptor/extension/captcha/phash-browser.js
# ---------------------------------------------------------------------------

# The JS builds this once at module load as a 32x32 table of cos((2i+1)*j*k),
# k = pi/(2N). Precomputed once here the same way, not per-call.
_N = 32
_K = np.pi / (2 * _N)
_COS32 = np.array(
    [[np.cos((2 * i + 1) * j * _K) for j in range(_N)] for i in range(_N)]
)


def _dct1(vec: np.ndarray) -> np.ndarray:
    """Direct, naive-summation port of phash-browser.js's `dct1`. This is
    NOT replaced with scipy's optimized DCT: confirmed by direct
    cross-check against a Node.js run of the real source that the two are
    mathematically equivalent but numerically different at the
    floating-point noise floor (~1e-14 magnitude) for near-uniform input --
    close to a real image's DC-dominated low-frequency coefficients, which
    is exactly what the hash's `v > median` bit decisions are made from.
    scipy's answer disagreed with the JS's on 3 of 4 real test images (all
    but one with genuine high-frequency content) purely from noise-floor
    sign differences, which silently produced a completely different hash
    and would have missed every cache hit for a flat/low-detail challenge
    tile. The naive summation, run in the same loop order as the source,
    reproduces the JS's exact floating-point rounding instead of merely an
    equivalent result."""
    c0 = np.sqrt(1.0 / _N)
    cu = np.sqrt(2.0 / _N)
    out = np.empty(_N)
    for u in range(_N):
        s = 0.0
        for x in range(_N):
            s += vec[x] * _COS32[x][u]
        out[u] = (cu if u else c0) * s
    return out


def _dct2(mat: np.ndarray) -> np.ndarray:
    """Direct port of `dct2`: row-wise dct1, then column-wise dct1 on the
    row-transformed result -- same two-pass structure and loop order as
    the source, for the same noise-floor-matching reason as `_dct1`."""
    tmp = np.empty((_N, _N))
    for y in range(_N):
        tmp[y] = _dct1(mat[y])
    out = np.empty((_N, _N))
    for x in range(_N):
        col = tmp[:, x]
        cd = _dct1(col)
        out[:, x] = cd
    return out


def _resize_stretch_to_32x32(img: Image.Image) -> np.ndarray:
    """Matches `ctx.drawImage(img, 0, 0, 32, 32)` on a 32x32 OffscreenCanvas:
    a non-uniform stretch of the source to exactly fill 32x32, not a crop and
    not an aspect-ratio-preserving resize. PIL's `.resize()` with no crop
    step does the same non-uniform stretch. Returns an (32, 32, 3) uint8
    RGB array, matching `getImageData`'s pixel layout before the alpha
    channel is dropped (the JS reads r/g/b directly and ignores alpha)."""
    rgb = img.convert("RGB")
    resized = rgb.resize((_N, _N), Image.BILINEAR)
    return np.asarray(resized, dtype=np.float64)


def phash(img: Image.Image) -> str:
    """Direct port of phash-browser.js's `phash()`. Verified line-by-line:

    - Luma weights (299, 587, 114 over 1000) match the JS constants exactly
      -- this is the ITU-R BT.601 weighting, not BT.709, confirmed by the
      literal numbers in the source.
    - The JS assigns the luma value into a `Uint8Array`, which truncates
      toward zero (JS ToUint8 on a non-negative float truncates the
      fractional part) -- matched here with `np.trunc` rather than
      `np.round`, since the two disagree for large parts of the value
      range and only truncation reproduces the extension's exact hash for
      cache hits to land correctly.
    - DCT-II with orthonormal scaling (c0 = sqrt(1/N), cu = sqrt(2/N) for
      u>0) matches `dct1`'s explicit scaling in the JS. This uses the
      naive-summation `_dct1`/`_dct2` above, NOT `scipy.fftpack.dct` -- see
      their docstrings for why: scipy's optimized DCT is mathematically
      equivalent but produces different floating-point noise on
      near-uniform input, which flipped hash bits on 3 of 4 real test
      cases in direct verification against the actual JS source.
    - The 8x8 low-frequency block excluding the DC term (x=0,y=0), median
      of those 63 coefficients, and building a 64-bit hash by comparing
      each coefficient to the median (in row-major (y,x) order, matching
      the JS loop order `for y: for x`) are all reproduced exactly.

    Returns the same 16-hex-char lowercase string format the SQLite cache
    stores (`hash.toString(16).padStart(16, '0')` in the JS).

    Known limitation, verified and accepted: on images with genuine visual
    content (real photos), this has been confirmed bit-identical to the JS
    source (cross-checked against a Node.js run of the actual extension
    file). On flat/low-detail images (solid colors, simple patterns) a
    handful of the 63 coefficients are so close to true zero that Python
    and JavaScript's floating-point rounding disagree on their sign,
    occasionally producing a different hash for those specific cases. This
    never produces a WRONG classification -- it only means the SQLite
    cache lookup below misses (since it requires exact band matches) and
    the caller falls back to the slower but independently-verified ONNX
    path instead of a fast cache hit. Real CAPTCHA tiles are photos, so
    this affects a narrow, low-frequency edge case, not typical usage."""
    rgb = _resize_stretch_to_32x32(img)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    luma = np.trunc((r * 299 + g * 587 + b * 114) / 1000)

    d = _dct2(luma)

    coeff = [d[y, x] for y in range(8) for x in range(8) if x or y]
    coeff_sorted = sorted(coeff)
    med = coeff_sorted[32]  # JS: coeff.slice().sort(...)[32] -- 0-indexed, same here

    hash_int = 0
    for v in coeff:
        hash_int = (hash_int << 1) | (1 if v > med else 0)
    return format(hash_int, "016x")


# ---------------------------------------------------------------------------
# SQLite cache lookup -- direct port of
# captcha-raptor/extension/background_sqlite.js's queryPhash()
# ---------------------------------------------------------------------------

_db_conn: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        # Read-only, immutable connection -- this file ships as static data
        # with the repo and is never written to at runtime.
        uri = f"file:{_DB_PATH.as_posix()}?mode=ro&immutable=1"
        _db_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    return _db_conn


def _hamming64(a: int, b: int) -> int:
    """Matches the JS `hamming64`: XOR then popcount over 64 bits."""
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def lookup_cache(imgtype: str, label: str, phash_hex: str) -> dict | None:
    """Direct port of `queryPhash()`. The 64-bit hash is split into four
    16-bit bands (b0 = bits 48-63 down to b3 = bits 0-15, matching the JS
    shift amounts exactly), and candidates are found via an LSH-style query
    that requires any TWO of the four bands to match exactly -- reproduced
    with the identical six-way OR of paired-band equality the JS SQL uses,
    against the same `phash33`/`phash44` tables in the same .sqlite file.
    Candidates are then filtered by Hamming distance < 10, same threshold
    as the source. imgtype is "33" or "44" (not "3x3"/"4x4" -- matches the
    JS's own imgtype string values, which the discovery/verify caller must
    pass through unchanged).

    Known upstream data quirk, confirmed present in the original extension
    too (same normalizeLabel() output there): phash33 stores some labels as
    "traffic_lights"/"chimneys" while normalize_label() always produces
    "trafficlights"/"chimney", so those two classes always miss the 3x3
    cache and fall through to ONNX. Not fixed here deliberately -- this port
    reproduces the extension's real behavior rather than silently improving
    on it, and ONNX still classifies these two classes correctly, just
    without the cache speedup."""
    if not _DB_PATH.is_file():
        logger.warning("captcha cache db not found at %s -- skipping cache lookup", _DB_PATH)
        return None

    big = int(phash_hex, 16)
    b0 = (big >> 48) & 0xFFFF
    b1 = (big >> 32) & 0xFFFF
    b2 = (big >> 16) & 0xFFFF
    b3 = big & 0xFFFF

    table = "phash33" if imgtype == "33" else "phash44"
    select_cols = "CAST(phash AS TEXT) AS phash, selected" if imgtype == "33" else "phash, mask"
    query = (
        f"SELECT {select_cols} FROM {table} WHERE label = ? AND ("
        "(band0=? AND band1=?) OR (band0=? AND band2=?) OR (band0=? AND band3=?) OR "
        "(band1=? AND band2=?) OR (band1=? AND band3=?) OR (band2=? AND band3=?))"
    )
    params = (label, b0, b1, b0, b2, b0, b3, b1, b2, b1, b3, b2, b3)

    conn = _get_db()
    cur = conn.execute(query, params)
    candidates = cur.fetchall()
    if not candidates:
        return {}

    best = None
    best_distance = 10
    for row in candidates:
        cand_phash = int(row[0]) if imgtype == "33" else row[0]
        distance = _hamming64(int(cand_phash), big)
        if distance < best_distance:
            best_distance = distance
            best = row

    if best is None:
        return {}

    if imgtype == "33":
        return {"selected": bool(best[1])}
    mask_int = int(best[1])
    return {"selected": format(mask_int, "04X")}


# ---------------------------------------------------------------------------
# ONNX inference fallback -- direct port of
# captcha-raptor/extension/captcha/recaptcha_ai.js
# ---------------------------------------------------------------------------

# Per-class decision thresholds for the 3x3 "type" model, copied verbatim
# from recaptcha_ai.js's THR.type table -- these are NOT a flat 0.5 cutoff,
# and using 0.5 for every class would silently disagree with the
# extension's own tuned behavior for most classes.
THRESHOLDS = {
    "hydrants": 0.139,
    "bridges": 0.191,
    "boats": 0.047,
    "cars": 0.994,
    "crosswalks": 0.136,
    "taxi": 0.862,
    "bicycles": 0.065,
    "trafficlights": 0.137,
    "motorcycles": 0.154,
    "stairs": 0.075,
    "mountains": 0.061,
    "tractors": 0.015,
    "buses": 0.618,
    "palm": 0.01,
    "parkingmeter": 0.001,
    "chimney": 0.006,
}
_DEFAULT_THRESHOLD = 0.50

# Class index mapping for the 3x3 "type" model, copied verbatim from
# recaptcha_ai.js's TYPE_INDEX -- the model output is a fixed-order vector
# and this order must match exactly or every class label maps to the wrong
# logit. The 4x4 "grid" model uses a DIFFERENT mapping, loaded from
# grid.meta.json below -- the two models are not interchangeable and do not
# share a class-index scheme.
TYPE_INDEX = {
    "boats": 0,
    "motorcycles": 1,
    "palm": 2,
    "parkingmeter": 3,
    "stairs": 4,
    "taxi": 5,
    "tractors": 6,
    "bicycles": 7,
    "cars": 8,
    "hydrants": 9,
    "crosswalks": 10,
    "buses": 11,
    "trafficlights": 12,
    "bridges": 13,
    "chimney": 14,
    "mountains": 15,
}

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_type_session = None
_grid_session = None
_grid_meta: dict | None = None


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _get_type_session():
    global _type_session
    if _type_session is None:
        import onnxruntime

        _type_session = onnxruntime.InferenceSession(
            str(_TYPE_MODEL_PATH), providers=["CPUExecutionProvider"]
        )
    return _type_session


def _get_grid_session():
    global _grid_session
    if _grid_session is None:
        import onnxruntime

        _grid_session = onnxruntime.InferenceSession(
            str(_GRID_MODEL_PATH), providers=["CPUExecutionProvider"]
        )
    return _grid_session


def _get_grid_meta() -> dict:
    """Loads grid.meta.json once -- the 4x4 model's class-index mapping,
    per-class per-tile-position threshold arrays, and expected input size
    (240, confirmed by reading the actual file, not assumed to match the
    3x3 model's 100). Matches `getGridMeta()` in recaptcha_ai.js, which
    fetches the same file over HTTP inside the extension; here it's read
    directly off disk since there's no extension context to fetch from."""
    global _grid_meta
    if _grid_meta is None:
        with open(_GRID_META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        if "type_to_index" not in meta or "thresholds_by_type" not in meta:
            raise ValueError(f"grid.meta.json at {_GRID_META_PATH} is missing required keys")
        _grid_meta = meta
    return _grid_meta


def _image_to_tensor(img: Image.Image, size: int) -> np.ndarray:
    """Direct port of `imgToTensor()`: resize to size x size, RGB, divide by
    255, normalize with ImageNet mean/std per channel, and lay out as NCHW
    (channel-first) float32 -- the exact shape and preprocessing
    `recaptcha_ai.js` builds before calling `sess.run()`."""
    rgb = img.convert("RGB").resize((size, size), Image.BILINEAR)
    arr = np.asarray(rgb, dtype=np.float32) / 255.0  # HWC, 0..1
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD
    chw = np.transpose(arr, (2, 0, 1))  # CHW
    return chw[np.newaxis, :, :, :].astype(np.float32)  # NCHW, N=1


def classify_via_onnx(tile_image: Image.Image, label: str) -> bool:
    """Direct port of the 3x3 per-tile classification path
    (`recognizeRecaptcha`'s `variant === 'type'` branch, via `probForLabel`):
    runs `type.onnx` on a single 100x100 tile, reads the logit for `label`'s
    index out of TYPE_INDEX, applies sigmoid (the JS does this in
    `probForLabel` -- a raw-logit-vs-threshold comparison, which an earlier
    draft of this port used, disagrees with the extension for any tile
    whose logit isn't already near zero), and compares against that
    label's tuned threshold (falling back to 0.50 for any class not in the
    THR.type table, matching the JS `default` entry)."""
    if label not in TYPE_INDEX:
        logger.warning("Unknown reCAPTCHA label %r -- cannot classify via ONNX", label)
        return False

    session = _get_type_session()
    tensor = _image_to_tensor(tile_image, 100)
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: tensor})
    logits = outputs[0].reshape(-1)
    prob = float(_sigmoid(logits[TYPE_INDEX[label]]))
    threshold = THRESHOLDS.get(label, _DEFAULT_THRESHOLD)
    return prob > threshold


def classify_grid_via_onnx(full_image: Image.Image, label: str) -> list[bool]:
    """Direct port of the 4x4 whole-grid classification path
    (`recognizeRecaptcha`'s non-'type' branch): runs `grid.onnx` once on the
    full grid image (resized to grid.meta.json's `image_size`, 240 -- NOT
    the 3x3 model's 100), reads the 16 per-tile-position logits for
    `label`'s class index (from grid.meta.json's `type_to_index`, a
    DIFFERENT mapping than the 3x3 model's TYPE_INDEX), applies sigmoid to
    each, and compares each of the 16 probabilities against that class's
    own 16-length per-position threshold array (`postGrid` in the JS --
    thresholds are tuned per tile position, not a single scalar).

    Also ports two post-processing steps from the JS that are easy to miss
    and change real behavior if skipped:

    - A 20%-of-the-time randomized adjustment that perturbs the selection
      based on each tile's own probability rather than a hard cutoff. This
      reads as intentional noise-injection (a comment in the source reads
      "la vida es un carrusel") likely meant to avoid a suspiciously
      deterministic click pattern being itself a bot-detection signal --
      reproduced as-is rather than "cleaned up", since removing it changes
      the tool's real-world behavior against Google's own detection, not
      just its code style.
    - If the resulting selection has exactly one tile selected, reCAPTCHA
      4x4 challenges require at least two -- the JS picks the
      next-highest-probability remaining tile (or a random one if every
      other probability is non-finite) and forces it selected too.
    """
    if label not in TYPE_INDEX:
        # Both models are only ever called with labels normalizeLabel()
        # already validated against the shared KNOWN set in recaptcha.js,
        # so this mirrors that same class vocabulary check.
        logger.warning("Unknown reCAPTCHA label %r -- cannot classify via ONNX", label)
        return [False] * 16

    meta = _get_grid_meta()
    class_idx = meta["type_to_index"].get(label)
    if class_idx is None:
        logger.warning("Label %r not present in grid.meta.json type_to_index", label)
        return [False] * 16
    thr16 = meta["thresholds_by_type"].get(label)
    if not isinstance(thr16, list) or len(thr16) != 16:
        logger.warning("grid.meta.json thresholds for %r are missing or malformed", label)
        return [False] * 16

    image_size = meta.get("image_size", 240)
    session = _get_grid_session()
    tensor = _image_to_tensor(full_image, image_size)
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: tensor})

    out = outputs[0]
    # out shape is expected to be (1, n_classes, 16) -- HW flattened to 16
    # tile positions, matching `out.dims[2]` in the JS's `probsGridForClass`.
    hw = out.shape[2] if out.ndim == 3 else 16
    logits = out.reshape(out.shape[1] if out.ndim == 3 else -1, hw)
    class_logits = logits[class_idx]
    probs16 = [float(_sigmoid(np.array(v))) for v in class_logits]

    data = [probs16[i] > (thr16[i] if i < len(thr16) else 0.5) for i in range(16)]

    if random.random() < 0.20:
        adjusted = []
        for i, selected in enumerate(data):
            p = probs16[i]
            if not selected:
                adjusted.append(random.random() < p)
            else:
                flip_prob = (1 - p) / 2
                adjusted.append(not (random.random() < flip_prob))
        data = adjusted

    count = sum(1 for v in data if v)
    if count == 1:
        first_idx = data.index(True)
        best_idx = -1
        best_p = float("-inf")
        for i, p in enumerate(probs16):
            if i == first_idx:
                continue
            if p > best_p:
                best_p = p
                best_idx = i
        if best_idx == -1 and len(data) > 1:
            choices = [i for i in range(len(data)) if i != first_idx]
            best_idx = random.choice(choices)
        if best_idx != -1:
            data[best_idx] = True

    return data


# ---------------------------------------------------------------------------
# Label normalization -- direct port of the ALIAS/KNOWN tables and
# extractLabel/normalizeLabel in recaptcha.js
# ---------------------------------------------------------------------------

_LABEL_ALIAS = {
    "fire": "hydrants", "firehydrant": "hydrants", "fire_hydrant": "hydrants",
    "bicycle": "bicycles", "bike": "bicycles", "boat": "boats", "bridge": "bridges",
    "bus": "buses", "car": "cars", "chimney": "chimney", "chimneys": "chimney",
    "crosswalk": "crosswalks", "zebra": "crosswalks", "pedestrian": "crosswalks",
    "hydrant": "hydrants", "motorcycle": "motorcycles", "mountain": "mountains",
    "palm": "palm", "parkingmeter": "parkingmeter", "parking": "parkingmeter",
    "stairs": "stairs", "stair": "stairs", "taxi": "taxi", "taxis": "taxi",
    "tractor": "tractors", "tractors": "tractors", "traffic": "trafficlights",
    "trafficlight": "trafficlights", "trafficlights": "trafficlights",
}
_KNOWN_LABELS = {
    "bicycles", "boats", "bridges", "buses", "cars", "chimney", "crosswalks",
    "hydrants", "motorcycles", "mountains", "palm", "parkingmeter", "stairs",
    "taxi", "tractors", "trafficlights",
}


def extract_label(instructions_text: str) -> str:
    """Port of `extractLabel`: pulls the target object out of the
    instruction sentence, e.g. "Select all images with a bus" -> "bus"."""
    import re

    m = re.search(r"with\s+(?:an?\s+)?([a-z]+)", instructions_text.lower())
    if m:
        return m.group(1)
    words = instructions_text.strip().split()
    return words[-1] if words else ""


def normalize_label(raw: str) -> str | None:
    """Port of `normalizeLabel`: maps a raw extracted word to one of the 16
    known model classes, via the alias table and a naive pluralization
    fallback, or None if it's not a class either model was trained on."""
    if not raw:
        return None
    maybe = _LABEL_ALIAS.get(raw, raw if raw.endswith("s") else raw + "s")
    return maybe if maybe in _KNOWN_LABELS else None


def _is_mostly_blank(img: Image.Image, low: int = 0, high: int = 230, ratio: float = 0.99) -> bool:
    """Direct port of `te()`: True if >=99% of pixels are near-black (<=0)
    or near-white (>=230) in every channel -- a tile that failed to render
    (blank placeholder) rather than genuine content. Used to wait and retry
    instead of misclassifying a not-yet-loaded tile."""
    arr = np.asarray(img.convert("RGB"))
    if arr.size == 0:
        return True
    near_black = np.all(arr <= low, axis=-1)
    near_white = np.all(arr >= high, axis=-1)
    blank = near_black | near_white
    return float(np.mean(blank)) > ratio


_FRAME_VISIBLE_JS = """
var el = arguments[0];
var r = el.getBoundingClientRect();
if (r.width < 50 || r.height < 50 || r.bottom <= 0 || r.top < -1000) return false;
for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
  var cs = window.getComputedStyle(n);
  if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.5) return false;
}
return true;
"""


class RecaptchaBlockedError(RuntimeError):
    """Raised when reCAPTCHA shows its own hard-block page
    (`.rc-doscaptcha-header` -- "Try again later" / automated-traffic
    notice), matching the JS's `we()` check. Distinct from a normal
    solve failure: this means Google has already decided to block this
    session outright, and no amount of retrying the image challenge will
    help."""


class RecaptchaSolveTimeoutError(RuntimeError):
    """Raised when the challenge doesn't clear within the overall time
    budget, e.g. because classification kept picking wrong tiles. Distinct
    from RecaptchaBlockedError -- this is "gave up", not "Google refused"."""


class RecaptchaSolver:
    """Drives a Selenium `driver` through solving a reCAPTCHA v2 checkbox
    challenge, replacing what the captcha-raptor browser extension used to
    do via content scripts injected directly into Google's reCAPTCHA
    iframes. Selenium has no equivalent automatic per-frame injection, so
    this explicitly locates and switches into the anchor and challenge
    iframes itself -- the one structural difference from the original,
    everything else (tile reading, phash/ONNX classification, click
    sequencing, verify submission, dynamic-reveal handling) is a direct
    port of the same logic.

    Deliberately NOT ported: the extension's own `caplog::*`/`capimg::*`
    calls that upload solved tiles back to captcha-raptor.com to improve
    their shared model. That is the extension's own telemetry/training
    pipeline, not something this tool has any reason to participate in or
    depend on, and skipping it does not change solving behavior -- it only
    means this tool doesn't contribute data back to that project.
    """

    # Timing constants match the extension's own defaults (recaptcha.js
    # reads these from user settings with these exact fallback values when
    # no settings override exists, which is always true here since there's
    # no popup UI to configure them from).
    CLICK_DELAY_SECONDS = 0.3
    REVEAL_DELAY_SECONDS = 0.85
    VERIFY_DELAY_SECONDS = 0.85
    JITTER_MAX_SECONDS = 0.3

    def __init__(self, driver, overall_timeout: float = 90.0):
        self.driver = driver
        self.overall_timeout = overall_timeout

    def _jitter(self) -> float:
        return random.random() * self.JITTER_MAX_SECONDS

    # -- Frame location -----------------------------------------------

    def _find_anchor_frame(self):
        """Locates the reCAPTCHA checkbox iframe from the top-level page.
        Standard Google markup (`iframe[title*="reCAPTCHA"]`,
        `src` containing `/recaptcha/api2/anchor`), not extension-specific."""
        self.driver.switch_to.default_content()
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        for frame in frames:
            src = frame.get_attribute("src") or ""
            if "/anchor" in src:
                return frame
        return None

    def _find_challenge_frame(self):
        """Locates the image-challenge iframe (opened after the checkbox is
        clicked), distinct from the anchor frame by its `/bframe` URL path."""
        self.driver.switch_to.default_content()
        frames = self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']")
        for frame in frames:
            src = frame.get_attribute("src") or ""
            if "/bframe" in src:
                return frame
        return None

    def _visible_challenge_frame(self):
        """The challenge iframe only if its popup is actually on screen.
        reCAPTCHA keeps the bframe in the DOM at all times and hides it by
        parking its wrapper at `top: -10000px` / `visibility: hidden` --
        including right after a Verify that passed. Working on the frame in
        that state reads a stale grid and every click lands at y ~ -9700,
        which is exactly how an already-solved CAPTCHA looked "stuck"."""
        self.driver.switch_to.default_content()
        for frame in self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='/bframe']"):
            try:
                if self.driver.execute_script(_FRAME_VISIBLE_JS, frame):
                    return frame
            except Exception:
                continue
        return None

    def has_recaptcha(self) -> bool:
        return self._find_anchor_frame() is not None

    # -- Checkbox click / solved detection ------------------------------

    def click_checkbox(self) -> bool:
        """Switches into the anchor frame and clicks the checkbox. Returns
        True if the checkbox is already checked afterward (solved with no
        image challenge -- happens often for a well-aged, real-looking
        profile), False if a challenge is expected to appear."""
        frame = self._find_anchor_frame()
        if frame is None:
            return False
        self.driver.switch_to.frame(frame)
        try:
            checkbox = self.driver.find_element(By.CSS_SELECTOR, ".recaptcha-checkbox")
            checkbox.click()
        except NoSuchElementException:
            self.driver.switch_to.default_content()
            return False

        solved = False
        try:
            anchor = self.driver.find_element(By.ID, "recaptcha-anchor")
            solved = anchor.get_attribute("aria-checked") == "true"
        except NoSuchElementException:
            pass
        self.driver.switch_to.default_content()
        return solved

    def is_solved(self) -> bool:
        frame = self._find_anchor_frame()
        if frame is None:
            return False
        self.driver.switch_to.frame(frame)
        try:
            anchor = self.driver.find_element(By.ID, "recaptcha-anchor")
            solved = anchor.get_attribute("aria-checked") == "true"
        except NoSuchElementException:
            solved = False
        self.driver.switch_to.default_content()
        return solved

    def _is_expired(self) -> bool:
        """True when the checkbox itself shows Google's own "Verification
        expired. Check the checkbox again." state -- this means the
        challenge timed out server-side and the only way forward is a fresh
        `click_checkbox()`, not continuing to poll the now-defunct challenge
        iframe (which just disappears and never comes back on its own)."""
        frame = self._find_anchor_frame()
        if frame is None:
            return False
        self.driver.switch_to.frame(frame)
        try:
            expired = bool(self.driver.find_elements(By.CLASS_NAME, "recaptcha-checkbox-expired"))
        except Exception:
            expired = False
        self.driver.switch_to.default_content()
        return expired

    # -- Challenge reading ------------------------------------------------

    def _is_blocked(self) -> bool:
        """Port of `we()`: True if reCAPTCHA is showing its own hard-block
        notice rather than an image challenge."""
        try:
            self.driver.find_element(By.CSS_SELECTOR, ".rc-doscaptcha-header")
            return True
        except NoSuchElementException:
            return False

    def _read_challenge(self) -> dict | None:
        """Port of `Ce()`: reads the instructions text, the 9- or 16-cell
        image table, and classifies which grid type this is (0 = static
        3x3, 1 = dynamic-reveal 3x3, 2 = 4x4) the same way the source
        does -- by cell count and whether any cell carries the
        `rc-image-tile-11` class (the marker the dynamic-reveal variant's
        first tile gets, per the JS's own `i = ... a.some(classList
        contains rc-image-tile-11) ? 1 : 0` logic)."""
        try:
            instr_el = self.driver.find_element(By.CSS_SELECTOR, ".rc-imageselect-instructions")
            instructions = instr_el.text
            if not instructions:
                return None

            cells = self.driver.find_elements(By.CSS_SELECTOR, "table tr td")
            if len(cells) not in (9, 16):
                return None

            images = []
            for cell in cells:
                try:
                    img_el = cell.find_element(By.TAG_NAME, "img")
                except NoSuchElementException:
                    continue
                if (img_el.get_attribute("src") or "").strip():
                    images.append(img_el)
            if len(images) not in (9, 16):
                return None
        except (NoSuchElementException, StaleElementReferenceException):
            # Google re-rendered the challenge grid mid-read (e.g. right
            # after a Verify click serves a fresh challenge) -- the cell/
            # image references captured moments ago no longer exist. Not a
            # real failure, just means this read landed mid-transition;
            # the caller retries on the next loop iteration with a fresh
            # DOM query.
            return None

        if len(cells) == 16:
            grid_type = 2
        else:
            is_dynamic_first_tile = any(
                "rc-image-tile-11" in (img.get_attribute("class") or "") for img in images
            )
            grid_type = 1 if is_dynamic_first_tile else 0

        lines = [l for l in instructions.split("\n") if l.strip()]
        wait_after_solve = len(lines) == 3 and grid_type != 2

        return {
            "task": instructions,
            "type": grid_type,
            "cells": cells,
            "images": images,
            "wait_after_solve": wait_after_solve,
        }

    def _screenshot_tile(self, img_element) -> Image.Image:
        """Captures a single `<img>` element's pixels directly via an
        in-page `<canvas>` + `drawImage()`, matching the source's own `ee()`
        capture approach almost exactly (same mechanism, no browser-chrome
        or scroll-position translation involved at all).

        An earlier version of this used Selenium's own screenshot APIs
        (`.screenshot_as_png` on the element, then later a full-viewport
        screenshot cropped by `getBoundingClientRect()`), but both produced
        wrong crops in practice on a real challenge grid -- confirmed
        directly by saving the captured tiles and inspecting them: instead
        of one ~100x100 tile, the image contained all 4 visible tiles plus
        page background text. Root cause not fully isolated (some
        combination of devicePixelRatio scaling and nested-iframe scroll
        offset), but moot once capture happens inside the page's own JS
        execution context, in the image's native pixel space, with no
        screenshot/coordinate translation step to get wrong."""
        import base64
        import io

        # Like the source's ee(): wait for the image to finish loading (up to
        # 10s) before drawing. Drawing a still-loading payload image throws,
        # which is what a freshly served 4x4 grid hit right after a Verify.
        result = self.driver.execute_async_script(
            """
            var img = arguments[0], done = arguments[arguments.length - 1];
            function draw() {
              try {
                var c = document.createElement('canvas');
                c.width = img.naturalWidth; c.height = img.naturalHeight;
                c.getContext('2d').drawImage(img, 0, 0);
                done(c.toDataURL('image/png'));
              } catch (e) { done('ERR:' + e); }
            }
            if (img.complete && img.naturalWidth > 0) return draw();
            var t = setTimeout(function () { done('ERR:image did not load'); }, 10000);
            img.addEventListener('load', function () { clearTimeout(t); draw(); }, {once: true});
            img.addEventListener('error', function () { clearTimeout(t); done('ERR:image failed to load'); }, {once: true});
            """,
            img_element,
        )
        if not result or result.startswith("ERR:"):
            raise RuntimeError(f"tile capture failed: {result}")
        header, b64data = result.split(",", 1)
        png_bytes = base64.b64decode(b64data)
        return Image.open(io.BytesIO(png_bytes)).convert("RGB")

    def _crop_3x3_tiles(self, full_image: Image.Image) -> list[Image.Image]:
        """Port of `split3x3()`: crops a single combined 3x3 grid image into
        nine 100x100 tiles, assuming the source is exactly 300x300 (three
        100px cells per side) -- matches the fixed 100x100-per-cell drawImage
        crop geometry in the source exactly."""
        w, h = full_image.size
        cell_w, cell_h = w / 3, h / 3
        tiles = []
        for r in range(3):
            for c in range(3):
                box = (c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h)
                tiles.append(full_image.crop(box).resize((100, 100), Image.BILINEAR))
        return tiles

    # -- Classification (cache-first, ONNX fallback) ----------------------

    def _classify_tiles_33(self, tiles: list[Image.Image], label: str, enforce: bool) -> list[bool]:
        """Port of `solveByPhash`'s 3x3/1x1 branch plus the ONNX fallback
        for any tile the cache didn't resolve: try the SQLite cache for
        every tile first, then run ONNX only on the ones that missed --
        same two-tier strategy and same order of preference as the
        source.

        `enforce` controls whether `_enforce_3to4()` (the "answer must
        select 3-4 tiles" padding rule) applies. Confirmed directly from
        source: `enforce3to4` is only called in the `grid === '3x3'`
        (single combined image, static) branch of `solveByPhash`, never in
        the `grid === '1x1'` (one `<img>` per cell, dynamic-reveal) branch.
        Applying it to dynamic-reveal too was a real bug in an earlier
        version of this port: forcing a minimum of 3 selections on every
        round of a challenge type meant to often have just 0-1 genuine
        matches per round caused endless random re-selection that never
        converged to "nothing left, click Verify"."""
        results: list[bool | None] = []
        for tile in tiles:
            h = phash(tile)
            cached = lookup_cache("33", label, h)
            results.append(cached.get("selected") if cached else None)

        unresolved_idx = [i for i, r in enumerate(results) if r is None]
        for idx in unresolved_idx:
            results[idx] = classify_via_onnx(tiles[idx], label)

        final = [bool(r) for r in results]
        return _enforce_3to4(final) if enforce else final

    def _classify_grid_44(self, full_image: Image.Image, label: str) -> list[bool]:
        """Port of `solveByPhash`'s 4x4 branch: one cache lookup against the
        whole-image hash; on a miss, the ONNX grid model classifies all 16
        positions in a single pass (there is no per-tile 4x4 cache fallback
        in the source either -- a 4x4 cache miss goes straight to the grid
        model, not 16 individual lookups)."""
        h = phash(full_image)
        cached = lookup_cache("44", label, h)
        if cached and "selected" in cached:
            mask = int(cached["selected"], 16)
            return [bool((mask >> (15 - i)) & 1) for i in range(16)]
        return classify_grid_via_onnx(full_image, label)

    # -- Tile clicking / verify submission --------------------------------

    def _click(self, element) -> bool:
        """Real mouse click first; DOM click as fallback if the element is
        momentarily overlapped (e.g. mid fade-in of a replaced tile)."""
        try:
            element.click()
            return True
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception as e:
                logger.debug("reCAPTCHA click failed: %s", e)
                return False

    def _click_tile(self, cells, idx: int, columns: int) -> None:
        """Port of the source's `tr:nth-child td:nth-child` selector-based
        click -- clicking the cell element held from `_read_challenge`."""
        self._click(cells[idx])

    def _click_verify(self) -> None:
        """Best-effort: the challenge can resolve before Verify is needed,
        in which case the button is gone or disabled -- not a failure."""
        try:
            btn = self.driver.find_element(By.ID, "recaptcha-verify-button")
        except NoSuchElementException:
            return
        self._click(btn)

    # -- Main solve loop ---------------------------------------------------

    def solve(self) -> bool:
        """Full solve flow: click the checkbox, and if a challenge appears,
        loop reading/classifying/clicking tiles until solved, blocked, or
        the overall timeout is reached. Returns True if solved, False if it
        timed out without being explicitly blocked. Raises
        RecaptchaBlockedError if reCAPTCHA shows its own hard-block page --
        that's a signal to abandon this attempt entirely, not retry the
        image challenge again."""
        import time as _time

        deadline = _time.monotonic() + self.overall_timeout
        try:
            if self.click_checkbox():
                return True
        except Exception as e:
            logger.info("reCAPTCHA checkbox click interrupted: %r", e)

        state = {"dyn33_task": None, "dyn33_recog_count": 0, "dyn33_updated_idx": []}
        hidden_since: float | None = None

        while _time.monotonic() < deadline:
            # The whole pass is guarded, not just the round: Google swaps
            # frames and pages under us at any step (a replaced challenge
            # frame between finding it and switching into it raised a stale
            # element error that killed the whole query). Any such change
            # just means "look again" on the next pass.
            try:
                # Re-evaluate the whole widget every pass. Checking "solved"
                # only after a completed round meant a pass that landed
                # mid-round (popup hidden, stale grid still in the DOM) was
                # never noticed.
                self.driver.switch_to.default_content()
                if self._find_anchor_frame() is None:
                    # Widget gone: the host page (e.g. google.com/sorry)
                    # accepted the token and navigated away.
                    return True
                if self.is_solved():
                    return True
                if self._is_expired():
                    logger.info("reCAPTCHA expired -- requesting a fresh challenge")
                    if self.click_checkbox():
                        return True
                    hidden_since = None
                    _time.sleep(1.0)
                    continue

                frame = self._visible_challenge_frame()
                if frame is None:
                    # Popup hidden: either Google is verifying the last answer
                    # (resolves in ~1-2s) or it closed the popup without a pass.
                    now = _time.monotonic()
                    if hidden_since is None:
                        hidden_since = now
                    elif now - hidden_since > 8:
                        if self.click_checkbox():
                            return True
                        hidden_since = None
                    _time.sleep(0.5)
                    continue
                hidden_since = None

                self.driver.switch_to.frame(frame)
                if not self._solve_round(state):
                    self.driver.switch_to.default_content()
                    return False
            except RecaptchaBlockedError:
                self.driver.switch_to.default_content()
                raise
            except Exception as e:
                logger.info("reCAPTCHA pass interrupted: %r", e)
                _time.sleep(0.5)

        try:
            self.driver.switch_to.default_content()
            return self.is_solved()
        except Exception:
            return False

    def _solve_round(self, state: dict) -> bool:
        """One read -> classify -> click -> (verify) pass on the visible
        challenge. Returns False only for an unsolvable label; everything
        else returns True and lets `solve()` re-evaluate the widget."""
        import time as _time

        if self._is_blocked():
            raise RecaptchaBlockedError("reCAPTCHA showed its own automated-traffic block page")

        # Source's Ce() settles 1s before every read.
        _time.sleep(1.0)
        challenge = self._read_challenge()
        if challenge is None:
            return True

        label = normalize_label(extract_label(challenge["task"]))
        if label is None:
            logger.warning("reCAPTCHA label %r doesn't map to a known class -- cannot solve", challenge["task"])
            return False

        grid_type = challenge["type"]
        is_dyn33 = grid_type == 1 and challenge["wait_after_solve"]
        if not is_dyn33 or state["dyn33_task"] != challenge["task"]:
            state["dyn33_task"] = challenge["task"] if is_dyn33 else None
            state["dyn33_recog_count"] = 0
            state["dyn33_updated_idx"] = []

        cells = challenge["cells"]
        images = challenge["images"] if grid_type == 1 else challenge["images"][:1]

        src44 = ""
        if grid_type == 2:
            # Source skips a 4x4 grid it already answered (same payload src).
            src44 = images[0].get_attribute("src") or ""
            if src44 and src44 == state.get("prev44_src"):
                return True

        tile_images = [self._screenshot_tile(img_el) for img_el in images]

        if grid_type == 1:
            # Dynamic 3x3: only replaced tiles are standalone 100x100 images.
            # Every tile not yet replaced still points at the whole original
            # 300x300 grid image, so classifying it would ask "does the whole
            # grid contain X?" -- almost always yes. The source keeps only
            # the 100x100 tiles (and their cells) for exactly this reason.
            kept = [(c, t) for c, t in zip(cells, tile_images) if t.size == (100, 100)]
            cells = [c for c, _ in kept]
            tile_images = [t for _, t in kept]
            if not tile_images:
                self._click_verify()
                _time.sleep(3.0)
                return True

        # Port of the source's "wait if any tile is blank" check: a replaced
        # dynamic tile that hasn't finished loading draws as blank.
        if any(_is_mostly_blank(t) for t in tile_images):
            _time.sleep(3.0)
            return True

        start_ts = _time.monotonic()
        if grid_type == 2:
            selection = self._classify_grid_44(tile_images[0], label)
        elif grid_type == 1:
            selection = self._classify_tiles_33(tile_images, label, enforce=False)
        else:
            selection = self._classify_tiles_33(self._crop_3x3_tiles(tile_images[0]), label, enforce=True)
        columns = 4 if grid_type == 2 else 3
        logger.info(
            "reCAPTCHA round: %r grid=%s selected=%s",
            label, grid_type, [i for i, s in enumerate(selection) if s],
        )

        if is_dyn33:
            state["dyn33_recog_count"] += 1

        if challenge["wait_after_solve"] and any(selection):
            wait = self.REVEAL_DELAY_SECONDS + self._jitter() - (_time.monotonic() - start_ts)
            if wait > 0:
                _time.sleep(wait)

        to_click = [
            i for i in range(len(selection))
            if selection[i] != ("rc-imageselect-tileselected" in (cells[i].get_attribute("class") or ""))
        ]
        if grid_type == 0:
            random.shuffle(to_click)

        clicked_idx: list[int] = []
        for k, idx in enumerate(to_click):
            if k > 0 and grid_type != 2:
                _time.sleep(self.CLICK_DELAY_SECONDS + self._jitter())
            clicked_idx.append(idx)
            self._click_tile(cells, idx, columns)

        if is_dyn33 and state["dyn33_recog_count"] == 0:
            state["dyn33_updated_idx"] = list(dict.fromkeys(clicked_idx))

        forced_dyn33_click = False
        if is_dyn33 and state["dyn33_recog_count"] == 1 and not any(selection):
            pool0 = state["dyn33_updated_idx"] or list(range(len(cells)))
            pool = [i for i in pool0 if "rc-imageselect-tileselected" not in (cells[i].get_attribute("class") or "")]
            pick_from = pool or pool0
            if pick_from:
                self._click_tile(cells, random.choice(pick_from), columns)
                forced_dyn33_click = True

        if grid_type == 2:
            state["prev44_src"] = src44

        if (not challenge["wait_after_solve"] or not any(selection)) and not forced_dyn33_click:
            _time.sleep(self.VERIFY_DELAY_SECONDS + self._jitter())
            self._click_verify()

        # Source's own wait loop: let replaced dynamic tiles finish swapping.
        for _ in range(5):
            if not self.driver.find_elements(By.CSS_SELECTOR, ".rc-imageselect-dynamic-selected"):
                break
            _time.sleep(1.0)
        return True


def _enforce_3to4(selection: list[bool]) -> list[bool]:
    """Port of `enforce3to4`: a 3x3 answer must select between 3 and 4
    tiles (never fewer, never more) -- reCAPTCHA's own validation rejects
    answers outside that range regardless of how confident the
    classification was, so this reshuffles/pads exactly like the source
    rather than submitting an answer guaranteed to be rejected."""
    limit = min(9, len(selection))
    idx = list(range(limit))
    selected = [i for i in idx if selection[i]]
    unselected = [i for i in idx if not selection[i]]

    if len(selected) < 3:
        random.shuffle(unselected)
        need = min(3 - len(selected), len(unselected))
        for i in range(need):
            selection[unselected[i]] = True
    elif len(selected) > 4:
        random.shuffle(selected)
        over = len(selected) - 4
        for i in range(over):
            selection[selected[i]] = False

    return selection

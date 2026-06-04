"""


Normalisation chain (applied before every TTS call, never to display text):
  1. Markdown noise strip     — headers, bold, italic, bullets, code blocks
  2. Time expansion           — 22:30 → "ten thirty PM"
  3. Temperature expansion    — 18°C → "eighteen degrees Celsius"
  4. Currency expansion       — €15 → "fifteen euros"
  5. Unit / measure expansion — 10km → "ten kilometres"
  6. Decimal expansion        — 1.7  → "one point seven"
  7. Integer expansion        — 42   → "forty-two"
  8. Abbreviation expansion   — API, URL, St. → Street, Ave → Avenue, etc.
  9. URL / fragment strip     — links replaced with "link provided below"
 10. Cleanup                  — duplicate phrases, stray punctuation
"""

import re
import time
import asyncio
from typing import Any, Callable, AsyncGenerator, List, Optional
from APP.modules.speech_to_text.stt_model import STTservice
from APP.modules.text_to_speech.tts_model import TTSservice


# ═══════════════════════════════════════════════════════════
# SECTION 1 — NUMBER-TO-WORDS ENGINE
# ═══════════════════════════════════════════════════════════

_ONES = [
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS  = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALE = ["", "thousand", "million", "billion", "trillion"]


def _int_below_1000(n: int) -> str:
    if n == 0:  return ""
    if n < 20:  return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if _ONES[n % 10] else "")
    hundreds = _ONES[n // 100] + " hundred"
    rest = _int_below_1000(n % 100)
    return hundreds + (" " + rest if rest else "")


def _integer_to_words(n: int) -> str:
    if n == 0:  return "zero"
    if n < 0:   return "negative " + _integer_to_words(-n)
    parts: List[str] = []
    scale_index = 0
    while n > 0:
        chunk = n % 1000
        if chunk:
            word = _int_below_1000(chunk)
            if _SCALE[scale_index]:
                word += " " + _SCALE[scale_index]
            parts.append(word)
        n //= 1000
        scale_index += 1
    return " ".join(reversed(parts))


def _number_to_words(value: str) -> str:
    value = value.strip().lstrip("+")
    negative = value.startswith("-")
    if negative: value = value[1:]
    if "." in value:
        integer_part, decimal_part = value.split(".", 1)
        int_words = _integer_to_words(int(integer_part)) if integer_part else "zero"
        dec_words = " ".join(_ONES[int(d)] if int(d) < len(_ONES) else d for d in decimal_part)
        result    = int_words + " point " + dec_words
    else:
        result = _integer_to_words(int(value))
    return ("negative " + result) if negative else result


# ═══════════════════════════════════════════════════════════
# SECTION 2 — MARKDOWN NOISE STRIPPER
# ═══════════════════════════════════════════════════════════

_MD_HEADER      = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_MD_BOLD_ITALIC = re.compile(r'\*{1,3}([^*]+?)\*{1,3}')
_MD_UNDERLINE   = re.compile(r'_{1,2}([^_]+?)_{1,2}')
_MD_INLINE_CODE = re.compile(r'`[^`]+`')
_MD_CODE_BLOCK  = re.compile(r'```[\s\S]*?```')
_MD_BULLET      = re.compile(r'^\s*[-*•]\s+', re.MULTILINE)
_MD_NUMBERED    = re.compile(r'^\s*\d+\.\s+', re.MULTILINE)
_MD_HR          = re.compile(r'^\s*[-_*]{3,}\s*$', re.MULTILINE)
_MD_BLOCKQUOTE  = re.compile(r'^\s*>\s?', re.MULTILINE)


def _strip_markdown(text: str) -> str:
    text = _MD_CODE_BLOCK.sub(' ', text)
    text = _MD_HEADER.sub('', text)
    text = _MD_BOLD_ITALIC.sub(r'\1', text)
    text = _MD_UNDERLINE.sub(r'\1', text)
    text = _MD_INLINE_CODE.sub('', text)
    text = _MD_BULLET.sub('', text)
    text = _MD_NUMBERED.sub('', text)
    text = _MD_HR.sub('', text)
    text = _MD_BLOCKQUOTE.sub('', text)
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'^\s*\d+\.\s*$', '', text, flags=re.MULTILINE)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 3 — TIME NORMALISER
# ═══════════════════════════════════════════════════════════

_TIME_24 = re.compile(r'\b([01]?\d|2[0-3]):([0-5]\d)\b')
_TIME_12 = re.compile(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\s*(AM|PM|am|pm)\b')


def _expand_time(text: str) -> str:
    def _fmt(hour: int, minute: int, period: str) -> str:
        h = _integer_to_words(hour)
        if minute == 0: return f"{h} {period}"
        m = _integer_to_words(minute)
        if minute < 10: m = "oh " + m
        return f"{h} {m} {period}"

    def _r24(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        if h == 0:  return _fmt(12, mi, "AM")
        if h < 12:  return _fmt(h,  mi, "AM")
        if h == 12: return _fmt(12, mi, "PM")
        return _fmt(h - 12, mi, "PM")

    text = _TIME_12.sub(lambda m: _fmt(int(m.group(1)), int(m.group(2)), m.group(3).upper()), text)
    text = _TIME_24.sub(_r24, text)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 4 — TEMPERATURE NORMALISER
# ═══════════════════════════════════════════════════════════

_TEMP_PATTERN = re.compile(r'(-?\d+(?:\.\d+)?)\s*°\s*([CcFf])\b')
_DEGREE_WORD  = {'c': 'Celsius', 'f': 'Fahrenheit'}


def _expand_temperature(text: str) -> str:
    return _TEMP_PATTERN.sub(
        lambda m: f"{_number_to_words(m.group(1))} degrees {_DEGREE_WORD[m.group(2).lower()]}",
        text,
    )


# ═══════════════════════════════════════════════════════════
# SECTION 5 — CURRENCY NORMALISER
# ═══════════════════════════════════════════════════════════

_CURRENCY_BEFORE = re.compile(r'([$€£¥₹₩₺₽])(\d+(?:,\d{3})*(?:\.\d{1,2})?)')
_CURRENCY_NAMES  = {
    '$': 'dollar', '€': 'euro',  '£': 'pound', '¥': 'yen',
    '₹': 'rupee',  '₩': 'won',   '₺': 'lira',  '₽': 'ruble',
}


def _expand_currency(text: str) -> str:
    def _replace(m: re.Match) -> str:
        raw      = m.group(2).replace(',', '')
        currency = _CURRENCY_NAMES.get(m.group(1), 'currency unit')
        if raw not in ('1', '1.00'): currency += 's'
        return f"{_number_to_words(raw)} {currency}"
    return _CURRENCY_BEFORE.sub(_replace, text)


# ═══════════════════════════════════════════════════════════
# SECTION 6 — UNIT / MEASURE NORMALISER
# ═══════════════════════════════════════════════════════════

_UNIT_MAP = {
    r'km/h': 'kilometres per hour',    r'mph':     'miles per hour',
    r'km':   'kilometres',             r'mi':      'miles',
    r'ft':   'feet',                   r'yd':      'yards',
    r'nm':   'nautical miles',         r'cm':      'centimetres',
    r'mm':   'millimetres',            r'in':      'inches',
    r'kg':   'kilograms',              r'lbs?':    'pounds',
    r'oz':   'ounces',                 r'g':       'grams',
    r'mg':   'milligrams',             r'ml':      'millilitres',
    r'cl':   'centilitres',            r'dl':      'decilitres',
    r'l':    'litres',                 r'fl\s*oz': 'fluid ounces',
    r'Mbps': 'megabits per second',    r'Gbps':    'gigabits per second',
    r'kbps': 'kilobits per second',    r'GB':      'gigabytes',
    r'MB':   'megabytes',              r'KB':      'kilobytes',
    r'TB':   'terabytes',              r'GHz':     'gigahertz',
    r'MHz':  'megahertz',              r'Hz':      'hertz',
    r'hrs?': 'hours',                  r'mins?':   'minutes',
    r'secs?':'seconds',                r'ms':      'milliseconds',
    r'bpm':  'beats per minute',       r'rpm':     'revolutions per minute',
    r'%':    'percent',                r'°':       'degrees',
    # NOTE: bare 'm' (metres) intentionally excluded — too short, causes false matches
    # on words like "km", "pm", "am", "from", etc. Use "km" for distances instead.
}
_UNIT_PATTERNS = [
    (re.compile(r'(-?\d+(?:\.\d+)?)\s*' + abbr + r'\b', re.IGNORECASE), expansion)
    for abbr, expansion in _UNIT_MAP.items()
]


def _expand_units(text: str) -> str:
    for pattern, expansion in _UNIT_PATTERNS:
        text = pattern.sub(
            lambda m, exp=expansion: f"{_number_to_words(m.group(1))} {exp}", text
        )
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 7 — STANDALONE NUMBER NORMALISER
# ═══════════════════════════════════════════════════════════

_LARGE_NUM = re.compile(r'\b\d{1,3}(,\d{3})+\b')
_DECIMAL   = re.compile(r'\b(-?\d+\.\d+)\b')
_INTEGER   = re.compile(r'\b(-?\d+)\b')
_YEAR_RE   = re.compile(r'\b(1[89]\d{2}|20\d{2})\b')


def _expand_numbers(text: str) -> str:
    years: dict[str, str] = {}
    def _save_year(m: re.Match) -> str:
        key = f"__YEAR{len(years)}__"; years[key] = m.group(0); return key
    text = _YEAR_RE.sub(_save_year, text)
    text = _LARGE_NUM.sub(lambda m: _number_to_words(m.group(0).replace(',', '')), text)
    text = _DECIMAL.sub(lambda m: _number_to_words(m.group(1)), text)
    text = _INTEGER.sub(lambda m: _number_to_words(m.group(1)), text)
    for key, original in years.items():
        text = text.replace(key, original)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 8 — ABBREVIATION EXPANDER
# ═══════════════════════════════════════════════════════════
#
# ADDRESS ABBREVIATIONS — applied first so they take priority
# over general abbreviations.
#
# FIXES vs original:
#   • St. / St  → Street  (was: "Saint" — wrong for travel addresses)
#   • Rd. / Rd  → Road    (was: r'\bRd,\.\b' — comma typo, never matched)
#   • Ave / Ave.→ Avenue  (was fine; confirmed working)
#   • Blvd      → Boulevard (was fine)
#   • Removed the erroneous r'\bSt,\b' pattern (comma typo, never matched)
# ─────────────────────────────────────────────────────────

_ADDRESS_ABBR_MAP = {
    # Street suffixes — order matters: longer/dotted patterns first
    r'\bSt\.\b':    'Street',       # St. → Street  (FIX: was "Saint")
    r'\bSt\,\b':    'Street',       # St, → Street  (FIX: was missing / had comma typo)
    r'\bSt\b':      'Street',       # St  → Street  (FIX: was missing / had comma typo)
    r'\bAve?\.\b':  'Avenue',       # Ave. → Avenue
    r'\bAve?\,\b': 'Avenue',       # Ave, → Avenue
    r'\bAve\b':     'Avenue',       # Ave  → Avenue  (confirmed working)
    r'\bBlvd\.?\b': 'Boulevard',    # Blvd / Blvd.
    r'\bRd\.?\b':   'Road',         # Rd / Rd.  (FIX: was r'\bRd,\.\b' — comma typo)
    r'\bRd\,\b':   'Road',         # Rd, → Road  (FIX: was r'\bRd,\.\b' — comma typo)
    r'\bDr\.?\b':   'Drive',        # Dr / Dr.
    r'\bLn\.?\b':   'Lane',         # Ln / Ln.
    r'\bCt\.?\b':   'Court',        # Ct / Ct.
    r'\bPl\.?\b':   'Place',        # Pl / Pl.
    r'\bFwy\.?\b':  'Freeway',      # Fwy / Fwy.
    r'\bHwy\.?\b':  'Highway',      # Hwy / Hwy.
    r'\bPkwy\.?\b': 'Parkway',      # Pkwy / Pkwy.
    r'\bN\.?\b':    'North',        # N / N.  directional
    r'\bS\.?\b':    'South',        # S / S.
    r'\bE\.?\b':    'East',         # E / E.
    r'\bW\.?\b':    'West',         # W / W.
    r'\bNE\.?\b':   'Northeast',
    r'\bNW\.?\b':   'Northwest',
    r'\bSE\.?\b':   'Southeast',
    r'\bSW\.?\b':   'Southwest',
}
_ADDRESS_ABBR_PATTERNS = [
    (re.compile(abbr), expansion) for abbr, expansion in _ADDRESS_ABBR_MAP.items()
]

_ABBR_MAP = {
    r'\bAPI\b':   'A P I',          r'\bURL\b':   'U R L',
    r'\bUI\b':    'U I',            r'\bUX\b':    'U X',
    r'\bAI\b':    'A I',            r'\bML\b':    'M L',
    r'\bSQL\b':   'S Q L',          r'\bHTTPS\b': 'H T T P S',
    r'\bHTTP\b':  'H T T P',        r'\bJSON\b':  'J S O N',
    r'\bHTML\b':  'H T M L',        r'\bCSS\b':   'C S S',
    r'\bID\b':    'I D',            r'\bDNA\b':   'D N A',
    r'\bRNA\b':   'R N A',          r'\bCPU\b':   'C P U',
    r'\bGPU\b':   'G P U',          r'\bRAM\b':   'ram',
    r'\bSSD\b':   'S S D',          r'\bVPN\b':   'V P N',
    r'\bPDF\b':   'P D F',          r'\bETA\b':   'E T A',
    r'\bFYI\b':   'F Y I',          r'\bASAP\b':  'A S A P',
    r'\bDOB\b':   'date of birth',  r'\bDOA\b':   'D O A',
    r'\bQ(\d)\b': r'quarter \1',    r'\be\.g\.\b':'for example',
    r'\bi\.e\.\b':'that is',        r'\betc\.\b': 'et cetera',
    r'\bvs\.\b':  'versus',         r'\bvs\b':    'versus',
    r'\bMr\.\s':  'Mister ',        r'\bMrs\.\s': 'Missus ',
    r'\bMs\.\s':  'Miss ',          r'\bDr\.\s':  'Doctor ',
    r'\bProf\.\s':'Professor ',     r'\bInc\.\b': 'Incorporated',
    r'\bLtd\.\b': 'Limited',
}
_ABBR_PATTERNS = [(re.compile(abbr), expansion) for abbr, expansion in _ABBR_MAP.items()]


def _expand_abbreviations(text: str) -> str:
    # Address abbreviations first (higher priority, more specific)
    for pattern, expansion in _ADDRESS_ABBR_PATTERNS:
        text = pattern.sub(expansion, text)
    # General abbreviations second
    for pattern, expansion in _ABBR_PATTERNS:
        text = pattern.sub(expansion, text)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 9 — URL / LINK SANITISER
# ═══════════════════════════════════════════════════════════

_URL_FRAGMENT_PATTERN = re.compile(
    r'\b\S*(?:place_id|query_id|api=|maps\.google|goo\.gl|bit\.ly)\S*\b', re.IGNORECASE
)
_URL_PATTERN = re.compile(
    r'https?://[^\s\)\]\>\"\']+ | www\.[^\s\)\]\>\"\']+', re.IGNORECASE | re.VERBOSE
)
_MARKDOWN_LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(https?://[^\)]+\)', re.IGNORECASE)


def _strip_urls(text: str) -> str:
    # Markdown links → keep the label text, drop the URL
    text = _MARKDOWN_LINK_PATTERN.sub(r'\1', text)
    # Bare URLs → replace with spoken placeholder
    text = _URL_PATTERN.sub('link provided below', text)
    # Leftover URL fragments (place_id=, %XX encoding, long token strings)
    text = _URL_FRAGMENT_PATTERN.sub('', text)
    text = re.sub(r'\S*%[0-9A-Fa-f]{2}\S*', '', text)
    text = re.sub(r'\S*=[A-Za-z0-9_\-]{8,}\S*', '', text)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 10 — CLEANUP
# ═══════════════════════════════════════════════════════════

def _cleanup(text: str) -> str:
    text = re.sub(r'(link provided below[\s,\.]*){2,}', 'link provided below ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'\s([,\.])', r'\1', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════
# SECTION 11 — MASTER NORMALISER
# ═══════════════════════════════════════════════════════════

def normalize_for_speech(text: str) -> str:
    """
    Full TTS normalisation pipeline.  Pure function, no side effects.
    NEVER call on display/UI text — only on strings going to TTS.
    """
    text = _strip_markdown(text)
    text = _expand_time(text)
    text = _expand_temperature(text)
    text = _expand_currency(text)
    text = _expand_units(text)
    text = _expand_numbers(text)
    text = _expand_abbreviations(text)
    text = _strip_urls(text)
    text = _cleanup(text)
    return text


# ═══════════════════════════════════════════════════════════
# SECTION 12 — VOICE PIPELINE
# ═══════════════════════════════════════════════════════════

_WORKER_DONE = object()   # sentinel: no more sentences coming

# Compiled once at module load — used only in _strip_urls and sentence
# boundary detection.  The partial-URL pattern is NOT used in
# _should_generate_audio (see fix note below).
_URL_IN_BUFFER = re.compile(r'https?://\S+', re.IGNORECASE)


class VoicePipeline:
    def __init__(self):
        self.stt_service = STTservice()
        self.tts_service = TTSservice()

    async def pipeline(
        self,
        audio_data: bytes,
        response_function: Callable[..., AsyncGenerator[str, None]],
        session_id: str,
        user_id: str,
        user_mood: str,
        stt_format: str = "wav",
        tts_voice: str  = "shimmer",
        tts_format: str = "mp3",
        **kwargs,
    ):
        """
        True simultaneous text + audio streaming.

        WHY PREVIOUS VERSIONS STILL DELAYED AUDIO
        ──────────────────────────────────────────
        Both prior approaches checked the audio queue only at yield points
        that the MAIN LOOP controlled (token boundaries or the drain after
        each token).  The problem: TTS takes ~500 ms; LLM tokens arrive
        every ~50 ms.  By the time TTS for sentence 1 finishes the LLM has
        already emitted every remaining token and the `async for` loop has
        exited — so audio always lands in Step 3 (after text).

        Even `while not audio_q.empty()` fails because during active text
        streaming the queue IS empty — TTS hasn't returned yet.

        THE FIX — asyncio.wait(FIRST_COMPLETED) racing
        ───────────────────────────────────────────────
        Instead of polling with a non-blocking check, we race two awaitables:

            next_text_task  = asyncio.create_task(text_iter.__anext__())
            next_audio_task = asyncio.create_task(audio_q.get())

            done, _ = await asyncio.wait({next_text_task, next_audio_task},
                                         return_when=FIRST_COMPLETED)

        The event loop blocks until EITHER a new text token arrives OR a TTS
        audio chunk lands in the queue — whichever happens first.  The winner
        is yielded immediately; both races are re-armed for the next iteration.

        Result: audio escapes the generator the instant TTS returns, even if
        the LLM is mid-stream.  Text and audio interleave in real time.

        Events yielded:
          {"type": "stt_output",  "text": str,   "latency": float, "timestamp": float}
          {"type": "agent_text",  "text": str}
          {"type": "tts_audio",   "audio": str,  "format": str, "latency": float,
                                  "timestamp": float, "chunk_index": int}
          {"type": "tts_error",   "error": str,  "chunk_index": int}
        """
        try:
            # ── Step 1: Speech-to-Text ──────────────────────────────
            stt_start   = time.time()
            transcript  = await self.stt_service.transcribe_speech(audio_data)
            stt_latency = time.time() - stt_start

            yield {
                "type":      "stt_output",
                "text":      transcript,
                "latency":   stt_latency,
                "timestamp": time.time(),
            }

            # ── Shared queues ───────────────────────────────────────
            sentence_q: asyncio.Queue = asyncio.Queue()  # main  → TTS worker
            audio_q:    asyncio.Queue = asyncio.Queue()  # TTS worker → main

            # ── TTS worker ──────────────────────────────────────────
            async def _tts_worker() -> None:
                chunk_index = 0
                while True:
                    sentence = await sentence_q.get()
                    if sentence is _WORKER_DONE:
                        break
                    try:
                        t0    = time.time()
                        audio = await self.tts_service.generate_speech(
                            sentence, tts_voice, tts_format
                        )
                        lat = time.time() - t0
                        await audio_q.put({
                            "type":        "tts_audio",
                            "audio":       audio,
                            "format":      tts_format,
                            "timestamp":   time.time(),
                            "latency":     lat,
                            "chunk_index": chunk_index,
                        })
                        print(f"✅ TTS chunk {chunk_index} ready ({lat:.2f}s)")
                    except Exception as exc:
                        print(f"⚠ TTS error on chunk {chunk_index}: {exc}")
                        await audio_q.put({
                            "type":        "tts_error",
                            "chunk_index": chunk_index,
                            "error":       str(exc),
                        })
                    chunk_index += 1
                await audio_q.put(None)  # done sentinel

            worker_task = asyncio.create_task(_tts_worker())

            # ── Step 2: Race text tokens vs audio chunks ────────────
            buffer         = ""
            text_exhausted = False
            audio_exhausted= False

            text_iter = response_function(
                session_id=session_id,
                user_input=transcript,
                user_mood=user_mood,
                user_id=user_id,
            ).__aiter__()

            next_text_task  = asyncio.create_task(text_iter.__anext__())
            next_audio_task = asyncio.create_task(audio_q.get())

            while not (text_exhausted and audio_exhausted):

                pending: set[asyncio.Task] = set()
                if not text_exhausted:
                    pending.add(next_text_task)
                if not audio_exhausted:
                    pending.add(next_audio_task)

                if not pending:
                    break

                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )

                for finished in done:

                    # ── Text token arrived ──────────────────────────
                    if finished is next_text_task:
                        try:
                            text_chunk = finished.result()
                            if text_chunk:
                                yield {"type": "agent_text", "text": text_chunk}
                                buffer += text_chunk

                                if self._should_generate_audio(buffer):
                                    sentence = buffer.strip()
                                    buffer   = ""
                                    if sentence:
                                        speech_text = normalize_for_speech(sentence)
                                        await sentence_q.put(speech_text)
                                        print(f"🔊 Queued: '{speech_text[:60]}…'")

                            next_text_task = asyncio.create_task(text_iter.__anext__())

                        except StopAsyncIteration:
                            text_exhausted = True
                            next_text_task = None

                            if buffer.strip():
                                speech_text = normalize_for_speech(buffer.strip())
                                await sentence_q.put(speech_text)
                                print(f"🔊 Final flush: '{speech_text[:60]}…'")

                            await sentence_q.put(_WORKER_DONE)
                            print("📭 LLM stream ended — worker signalled")

                    # ── Audio chunk arrived ─────────────────────────
                    elif finished is next_audio_task:
                        event = finished.result()
                        if event is None:
                            audio_exhausted = True
                            next_audio_task = None
                        else:
                            yield event
                            print(f"🎵 Audio chunk {event.get('chunk_index')} yielded mid-stream")
                            next_audio_task = asyncio.create_task(audio_q.get())

            # ── Cleanup ─────────────────────────────────────────────
            for t in [next_text_task, next_audio_task]:
                if t and not t.done():
                    t.cancel()

            await worker_task

        except Exception as exc:
            print(f"Error in voice pipeline: {exc}")
            raise

    # ── Sentence boundary heuristic ──────────────────────────────────
    #
    # FIX: The original code had _PARTIAL_URL_PATTERN as a hard guard:
    #
    #   if _PARTIAL_URL_PATTERN.search(buffer):
    #       return False
    #
    # This blocked ANY sentence that ended with a URL (e.g. a map link)
    # from EVER being flushed to TTS, because map links are always at the
    # end of a buffer chunk. Those sentences would accumulate in the buffer
    # indefinitely and only reach TTS in the final flush — causing a long
    # silence until the entire LLM response finished.
    #
    # The fix: remove the partial URL guard entirely. normalize_for_speech()
    # already strips all URLs before the text reaches the TTS engine, so
    # there is no need to delay boundary detection because of a URL.
    # Sentences with URLs are flushed normally; the URL becomes
    # "link provided below" in spoken audio.
    # ─────────────────────────────────────────────────────────────────

    def _should_generate_audio(self, buffer: str) -> bool:
        """Return True when the buffer contains a complete speech unit."""
        stripped = buffer.rstrip()

        # If the buffer ends with a URL, check the text that precedes it.
        # normalize_for_speech() strips URLs before TTS, so we only care
        # whether the spoken part (before the link) is a complete sentence.
        # FIX: The original code returned False for any buffer ending with a URL,
        # which permanently blocked every sentence that contained a map link.
        url_match = _URL_IN_BUFFER.search(stripped)
        if url_match:
            pre_url = stripped[:url_match.start()].rstrip()
            if pre_url:
                # Recurse on the spoken portion — if it reads as complete, flush.
                return self._should_generate_audio(pre_url)
            # URL with no preceding text — flush only if buffer is very long
            return len(buffer.split()) >= 15

        # Sentence-ending punctuation
        if re.search(r'[!?]$', stripped):
            return True

        if stripped.endswith('.'):
            # List-item numbers like "3." or "12." — not a sentence end.
            # Only block 1-2 digit numbers; 4-digit years (2026.) are sentence ends.
            if re.search(r'\b\d{1,2}\.$', stripped):
                return False
            # Known address suffix abbreviations at end of buffer ARE sentence ends.
            # Must check before the general abbreviation guard below because
            # patterns like St. / Rd. / Ave. match [A-Z][a-z]{0,3}\. and would
            # otherwise be incorrectly blocked.
            # Note: Dr (Drive) is intentionally excluded — it is ambiguous with
            # the title "Dr." (Doctor) which should NOT trigger a flush.
            if re.search(
                r'\b(St|Ave|Rd|Blvd|Ln|Ct|Pl|Fwy|Hwy|Pkwy|NE|NW|SE|SW)\.$',
                stripped,
            ):
                return True
            # Common title / mid-sentence abbreviations — NOT a sentence end
            if re.search(r'\b[A-Z][a-z]{0,3}\.$', stripped):
                return False
            return True

        # Long clause ending with comma — natural spoken pause
        if stripped.endswith(',') and len(buffer.split()) >= 8:
            return True

        # Hard word-count ceiling — flush regardless of punctuation
        if len(buffer.split()) >= 15:
            return True

        return False
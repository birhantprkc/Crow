"""Dictation for Crow's window: the microphone in, text out. Nothing is spoken.

WHY THIS IS PYTHON AND NOT THE PAGE. The window is handed to WebView2 as HTML
rather than served, so it is not a secure context -- measured 2026-08-13, when
`navigator.clipboard` refused silently and the copy button reported success
over an empty clipboard. `getUserMedia` sits behind that same gate, so the page
cannot reach a microphone at all. WebView2 has no recogniser of its own either:
caniwebview.com listed Speech Recognition for it without a version as of
2026-08-15, and the request in WebView2Feedback #1613 has stood open since
2021. So the page draws a button and this module does the work -- exactly the
split `Api.copy` already uses for the clipboard.

NOTHING REACHES THE DISK. `sounddevice` fills a numpy array and faster-whisper
takes one directly, so there is no WAV in between: no temp file to leak, and no
recording for anyone to forget to delete. The only thing read from disk here is
the model.

BOTH IMPORTS ARE OPTIONAL, the way pywebview is for the window itself.
`available()` NAMES what is missing and returns it as a sentence; it never
raises. A machine without a microphone is still a machine that runs Crow.

THE MODEL IS `small`, AND THAT IS A CEILING DECISION. robin's rule was no
gigabyte download. Measured against the HuggingFace API on 2026-08-21:
`small` 486.2 MB, `medium` 1531 MB, `turbo` 1622 MB, `large-v3` 3087 MB -- so
`small` is simply the largest rung that fits. Multilingual, and NOT one of the
`distil-*` or German-tuned checkpoints: most users speak English into this box
and robin speaks German into it, and a one-language model would have to be
chosen against one of them.
"""

from __future__ import annotations

import threading
from pathlib import Path

# 16 kHz mono is what Whisper's front end wants, and it is asked of the device
# directly. NOT the device default with a resampler behind it: a resampler this
# repo has never measured is a second thing that can be wrong about the audio,
# and it would be wrong quietly. A device that cannot do 16 kHz says so through
# PortAudio, and that message names the rate instead of hiding it.
SAMPLE_RATE = 16000
CHANNELS = 1

MODEL_NAME = "small"
MODEL_DIRNAME = "whisper-small"

# A recording nobody stopped grows until the machine notices. Five minutes of
# 16 kHz float32 is 19 MB, which is what a forgotten button is allowed to cost.
MAX_SECONDS = 300

# Under a fifth of a second is a mis-click, not a sentence.
MIN_FRAMES = SAMPLE_RATE // 5

_lock = threading.Lock()
_stream = None
_blocks: list = []
_model = None


def model_dir() -> Path:
    """Where install.ps1 puts the model: <crow>/models/whisper-small.

    One relative step serves both layouts, because the repo and an install both
    keep the client in `cli/` beside `models/`.
    """
    return Path(__file__).resolve().parent.parent / "models" / MODEL_DIRNAME


def available() -> str | None:
    """None when dictation can run; otherwise the reason, in one line.

    ORDER IS CHEAPEST FIRST. Asking PortAudio for the device list starts an
    audio host; there is no point paying for that to tell a user their problem
    is a missing package.
    """
    try:
        import sounddevice as sd
    except Exception:                      # noqa: BLE001 - reported, not raised
        return "dictation needs sounddevice -- pip install sounddevice"
    try:
        import faster_whisper                                     # noqa: F401
    except Exception:                      # noqa: BLE001 - reported, not raised
        return "dictation needs faster-whisper -- pip install faster-whisper"
    try:
        sd.query_devices(kind="input")
    except Exception as exc:               # noqa: BLE001 - reported, not raised
        return "no microphone: %s" % exc
    return None


def recording() -> bool:
    """True between start() and the stop() or cancel() that ends it."""
    return _stream is not None


def start() -> None:
    """Open the stream. PortAudio's own thread does the collecting.

    Calling this twice is a no-op rather than an error: the button that reaches
    here is a toggle, and a double click should not cost the first recording.
    """
    import sounddevice as sd

    global _stream
    with _lock:
        if _stream is not None:
            return
        _blocks.clear()
        held = {"frames": 0}
        limit = MAX_SECONDS * SAMPLE_RATE

        def take(indata, _frames, _time, _status):
            # COPY, because PortAudio hands the same buffer back every time.
            # Without it every block in the list is the last one, and a minute
            # of speech transcribes as its final 20 milliseconds.
            if held["frames"] >= limit:
                return
            held["frames"] += len(indata)
            _note_level(indata)
            _blocks.append(indata.copy())

        _stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                 dtype="float32", callback=take)
        _stream.start()


_level = 0.0


def _note_level(block) -> None:
    """Den lautesten Wert eines Blocks merken, fuer die Anzeige im Fenster.

    SPITZE STATT MITTEL. Ein RMS ueber 20 ms glaettet genau die Silben weg, die
    eine Anzeige sichtbar machen soll -- gesprochene Sprache ist Stille mit
    Ausschlaegen darin, und der Mittelwert davon steht fast still.

    KEIN ZWEITER STROM. Der Block ist der, den `take` ohnehin bekommt und
    kopiert; ein eigener Strom fuer den Pegel waere ein zweites Geraet, das
    sich weigern kann.

    EIN LEERER BLOCK IST KEIN FEHLER: der Treiber reicht einen durch, wenn er
    nachlaedt, und eine Ausnahme in PortAudios eigenem Faden nimmt die Aufnahme
    mit.
    """
    global _level
    try:
        _level = float(abs(block).max()) if len(block) else 0.0
        return
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        _level = max((abs(float(v)) for v in block), default=0.0)
    except (TypeError, ValueError):
        _level = 0.0


def level() -> float:
    """Der letzte Pegel, 0.0 sobald nichts mehr aufgenommen wird.

    NULL BEIM STILLSTAND, und das ist keine Formsache: eine Anzeige, die nach
    dem Stopp weiterzappelt, behauptet ein Mikrofon, das nicht mehr zuhoert.
    `cancel` und `stop` setzen ihn zurueck, nicht diese Funktion -- sie liest.
    """
    return _level


def cancel() -> None:
    """Drop the stream and everything collected. Nothing is transcribed."""
    global _stream, _level
    with _lock:
        stream, _stream = _stream, None
        _blocks.clear()
        _level = 0.0
    _close(stream)


def stop() -> str:
    """Close the stream and transcribe what was collected. Empty when silent."""
    global _stream
    with _lock:
        stream, _stream = _stream, None
        blocks, _blocks[:] = list(_blocks), []
    _close(stream)
    # BEFORE numpy IS EVEN IMPORTED. "Nothing was recorded" is an answer this
    # module can give on a machine that has none of the optional packages, and
    # a version that imported first turned a button pressed twice by accident
    # into an ImportError instead of a shrug.
    if not blocks:
        return ""
    import numpy as np

    audio = np.concatenate(blocks, axis=0).reshape(-1)
    if len(audio) < MIN_FRAMES:
        return ""
    segments, _info = load_model().transcribe(
        audio, beam_size=5,
        # VAD FIRST, because Whisper writes sentences over silence -- the
        # well-known "thank you for watching" on an empty clip. A button
        # pressed by accident has to paste nothing, not a subtitle. The
        # language is deliberately NOT pinned: it is detected per recording, so
        # an English user and a German one share one build and one setting.
        vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def load_model():
    """The model, loaded once and kept. Slow on the first call by design."""
    global _model
    if _model is not None:
        return _model
    from faster_whisper import WhisperModel

    where = model_dir()
    # THE INSTALLED COPY WINS. install.ps1 fetches the model next to the
    # client; only when that is absent does the bare name go to
    # faster-whisper, which downloads the same 486 MB into the HuggingFace
    # cache on first use. Naming the directory first keeps a repo checkout and
    # an install pointing at one file rather than two.
    source = str(where) if (where / "model.bin").exists() else MODEL_NAME
    # CPU AND int8, AND THE CARD IS THE REASON. At the operating point
    # manifests/runs-2026-08-20.json recorded 6935 MiB of VRAM free; a
    # recogniser that takes a slice of that competes with the very model the
    # user is dictating to. int8 on CPU cost 1m42s for 13 minutes of audio in
    # faster-whisper's own benchmark (small, beam 5, 8 threads, i7-12700K),
    # which is about 2.6 s for a 20-second dictation.
    _model = WhisperModel(source, device="cpu", compute_type="int8")
    return _model


def _close(stream) -> None:
    """Stop and close, and never raise: the caller is already on its way out."""
    if stream is None:
        return
    try:
        stream.stop()
        stream.close()
    except Exception:                      # noqa: BLE001 - a dead stream is closed
        pass

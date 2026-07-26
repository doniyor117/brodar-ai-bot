"""
Media extraction: turn a Telegram photo/video/GIF/voice note into content the
model can actually consume — JPEG frames for vision, mp3 for audio.

Everything here is async and bounded. The previous version ran a separate
blocking `subprocess.run` per frame inside `asyncio.to_thread`, so a single
10-frame video spawned 11 ffmpeg processes, each re-decoding the file from
scratch, occupying one of the default executor's 5 slots on a 1-vCPU box for up
to ~245s — and `to_thread` can't be cancelled, so a stuck ffmpeg was permanent.
`asyncio.create_subprocess_exec` involves no thread pool at all and can be
killed, so a hung ffmpeg costs one process and nothing else.

Uses the bundled binary from imageio-ffmpeg (installs cleanly on Render with no
system package), falling back to a system ffmpeg if present.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*: Audio:")
_VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*: Video:")

# Frames are only ever read by a vision model, so 384px on the long edge is
# plenty and roughly quarters the token cost versus 512.
DEFAULT_MAX_DIM = 384
# Absolute ceiling on ffmpeg wall time per invocation.
PROBE_TIMEOUT = 20.0
FRAMES_TIMEOUT = 120.0
AUDIO_TIMEOUT = 150.0
# If 16kHz mono FLAC comes out bigger than this (an unusually long or noisy
# clip), fall back to a lossy 96kbps encode rather than risk overrunning the
# turn's media byte budget or Gemini's 20MB inline request cap.
AUDIO_FLAC_MAX_BYTES = 8 * 1024 * 1024


@dataclass
class MediaInfo:
    """What a probe could determine about a file."""
    duration: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    ok: bool = False


@dataclass
class MediaItem:
    """
    One piece of extracted media, tagged with what it actually IS.

    The `kind` field is the whole point. Audio was previously appended to the
    same flat list of "urls" as images and then wrapped as {"type": "image_url"}
    by the agent, so every voice note the bot ever received was handed to the
    model labelled as a picture. It has never heard anything.
    """
    data_url: str
    kind: str = "image"        # "image" | "audio"
    is_gif: bool = False       # frame extracted from a video/gif, not a photo

    def size(self) -> int:
        return len(self.data_url)


@dataclass
class ExtractionResult:
    items: List[MediaItem] = field(default_factory=list)
    # User-facing problems worth mentioning ("that video's too long"), as opposed
    # to internal failures, which are logged.
    notes: List[str] = field(default_factory=list)

    def images(self) -> List[MediaItem]:
        return [i for i in self.items if i.kind == "image"]

    def audio(self) -> List[MediaItem]:
        return [i for i in self.items if i.kind == "audio"]


def ffmpeg_exe() -> Optional[str]:
    """Path to an ffmpeg binary, or None if unavailable."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


async def _run(cmd: List[str], timeout: float) -> tuple:
    """
    Run a subprocess with a hard timeout, killing it if it overruns.

    Returns (returncode, stdout, stderr). A timeout returns (-1, b"", b"") after
    the process has actually been killed — the old code captured stderr and threw
    it away, so every ffmpeg failure was completely invisible.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.error(f"Failed to spawn ffmpeg: {e}")
        return -1, b"", b""

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out, err
    except asyncio.TimeoutError:
        logger.warning(f"ffmpeg exceeded {timeout}s; killing it. cmd={cmd[1:4]}")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, b"", b""
    except asyncio.CancelledError:
        # The chat turn was cancelled (/stop, or a newer message). Don't leave an
        # orphan ffmpeg running.
        try:
            proc.kill()
        except Exception:
            pass
        raise


async def probe(path: str) -> MediaInfo:
    """
    Read duration and stream layout from a media file.

    ffprobe is not shipped by imageio-ffmpeg, so this parses `ffmpeg -i`'s
    stderr, which reports the same information. Knowing whether an audio stream
    exists is what lets us skip the audio pass entirely for GIFs and video
    stickers, which are always silent — the old code ran a full audio extraction
    on every one of them and always got nothing.
    """
    exe = ffmpeg_exe()
    if not exe:
        return MediaInfo()

    # `ffmpeg -i <file>` with no output exits non-zero by design; the metadata
    # we want is on stderr regardless.
    _, _, err = await _run([exe, "-hide_banner", "-i", path], PROBE_TIMEOUT)
    text = err.decode("utf-8", "replace")
    if not text:
        return MediaInfo()

    info = MediaInfo(ok=True)
    m = _DURATION_RE.search(text)
    if m:
        h, mn, s = m.groups()
        info.duration = int(h) * 3600 + int(mn) * 60 + float(s)
    info.has_audio = bool(_AUDIO_STREAM_RE.search(text))
    info.has_video = bool(_VIDEO_STREAM_RE.search(text))
    return info


async def extract_frames(
    path: str,
    count: int,
    max_dim: int = DEFAULT_MAX_DIM,
    duration: float = 0.0,
) -> List[bytes]:
    """
    Extract up to `count` evenly-spaced JPEG frames in ONE decode pass.

    The previous implementation seeked and decoded the file separately for every
    frame. Using an fps filter derived from the duration gets the same coverage
    from a single invocation, so a 10-frame video costs one process instead of
    ten.
    """
    exe = ffmpeg_exe()
    if not exe or count < 1:
        return []

    with tempfile.TemporaryDirectory() as d:
        pattern = os.path.join(d, "frame_%03d.jpg")
        scale = f"scale='min({max_dim},iw)':-2"

        if duration > 0.5 and count > 1:
            # count frames spread across the whole clip. The tiny epsilon keeps
            # the last frame from landing past the end and being dropped.
            rate = count / max(duration - 0.05, 0.1)
            vf = f"fps={rate:.6f},{scale}"
        else:
            # Too short (or unknown length) to space anything out — take frames
            # as they come and let -frames:v do the capping.
            vf = scale

        rc, _, err = await _run([
            exe, "-hide_banner", "-loglevel", "error",
            "-i", path,
            "-vf", vf,
            "-frames:v", str(count),
            "-q:v", "4",
            "-y", pattern,
        ], FRAMES_TIMEOUT)

        frames = []
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jpg"):
                continue
            fp = os.path.join(d, name)
            try:
                if os.path.getsize(fp) > 0:
                    with open(fp, "rb") as f:
                        frames.append(f.read())
            except Exception as e:
                logger.warning(f"Could not read extracted frame {name}: {e}")

        if not frames:
            logger.warning(
                f"Frame extraction produced nothing (rc={rc}): "
                f"{err.decode('utf-8', 'replace')[:400]}"
            )
        return frames[:count]


async def extract_audio(path: str, max_seconds: float = 0.0) -> tuple:
    """
    Normalize a file's audio to 16kHz mono FLAC — lossless, natively supported
    by Gemini's audio understanding, and small at the rate a speech model
    actually works at (~8-10 KB/s for speech). Telegram voice notes are Opus;
    the previous approach re-encoded that to 32kbps mono MP3, a lossy transcode
    of an already-lossy source that smeared exactly the consonant detail a
    speech model needs — which is why Uzbek speech was coming back as Turkish
    or as invented words.

    Falls back to a 96kbps mono MP3 (still 16kHz, still far better than the old
    32kbps) if the FLAC result overruns AUDIO_FLAC_MAX_BYTES, so an unusually
    long or noisy clip can't blow the turn's media byte budget.

    `max_seconds` caps the encoded length. Without a cap, a one-hour voice note
    became ~19MB of base64 that was then re-sent to the model on every turn for
    the whole retention window.

    Returns (data, mime) — mime is "" alongside b"" on total failure, so the
    data URL built from it always reflects what was actually encoded rather
    than a hard-coded guess.
    """
    exe = ffmpeg_exe()
    if not exe:
        return b"", ""

    with tempfile.TemporaryDirectory() as d:
        def _cmd(outp: str, *audio_args: str) -> List[str]:
            c = [exe, "-hide_banner", "-loglevel", "error", "-i", path]
            if max_seconds and max_seconds > 0:
                c += ["-t", f"{max_seconds:.2f}"]
            c += ["-vn", "-ar", "16000", "-ac", "1", *audio_args, "-y", outp]
            return c

        flac_path = os.path.join(d, "out.flac")
        rc, _, err = await _run(_cmd(flac_path, "-c:a", "flac"), AUDIO_TIMEOUT)

        try:
            size = os.path.getsize(flac_path) if os.path.exists(flac_path) else 0
        except Exception:
            size = 0

        if size > 0:
            if size <= AUDIO_FLAC_MAX_BYTES:
                try:
                    with open(flac_path, "rb") as f:
                        return f.read(), "audio/flac"
                except Exception as e:
                    logger.warning(f"Could not read extracted FLAC audio: {e}")
            else:
                logger.info(
                    f"FLAC audio came out {size} bytes, over the {AUDIO_FLAC_MAX_BYTES} "
                    "budget; falling back to a 96kbps mp3 encode."
                )
        else:
            logger.warning(
                f"FLAC audio extraction produced nothing (rc={rc}): "
                f"{err.decode('utf-8', 'replace')[:400]}"
            )

        mp3_path = os.path.join(d, "out.mp3")
        rc2, _, err2 = await _run(_cmd(mp3_path, "-b:a", "96k"), AUDIO_TIMEOUT)
        try:
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                with open(mp3_path, "rb") as f:
                    return f.read(), "audio/mp3"
        except Exception as e:
            logger.warning(f"Could not read extracted mp3 audio fallback: {e}")

        logger.warning(
            f"Audio extraction produced nothing at all (flac rc={rc}, mp3 rc={rc2}): "
            f"{err2.decode('utf-8', 'replace')[:400]}"
        )
        return b"", ""


def frame_count_for(duration: float, max_frames: int, seconds_per_frame: float) -> int:
    """
    How many frames to pull from a clip of this length.

    Always at least 1, at most max_frames. A short clip gets a handful; a long
    one gets max_frames spread across the whole thing rather than max_frames
    crammed into the opening seconds.
    """
    if duration <= 0:
        return min(3, max_frames)
    wanted = int(duration / max(seconds_per_frame, 0.1))
    return max(1, min(max_frames, max(3, wanted)))


async def write_temp(data: bytes, suffix: str = "") -> str:
    """Write bytes to a temp file and return the path. Caller must remove it."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass
        raise
    return path

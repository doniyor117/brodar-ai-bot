"""
Media helpers — turn a Telegram animation/GIF (a silent mp4) into a couple of
still JPEG frames so vision models can "see" it.

Uses ffmpeg. We prefer the bundled binary from imageio-ffmpeg (installs cleanly
on Render with no system package), and fall back to a system ffmpeg if present.
Everything here is blocking (subprocess) and must be called via asyncio.to_thread.
"""
import os
import re
import shutil
import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffmpeg_exe():
    """Path to an ffmpeg binary, or None if unavailable."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _probe_duration(exe: str, path: str) -> float:
    """Seconds of a media file, parsed from ffmpeg's stderr. 0.0 if unknown."""
    try:
        p = subprocess.run([exe, "-i", path], capture_output=True, text=True, timeout=15)
        m = _DURATION_RE.search(p.stderr or "")
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception as e:
        logger.warning(f"ffmpeg duration probe failed: {e}")
    return 0.0


def extract_video_frames(data: bytes, mode: str = "video", max_frames: int = 10, max_dim: int = 512) -> list:
    """
    Extract JPEG frames from an mp4/gif's bytes.
    If mode=="loop" (e.g. GIFs/Stickers), extracts 3 frames: start, middle, end.
    If mode=="video", extracts ~1 frame every 3 seconds evenly up to max_frames.
    Frames are downscaled to <= max_dim on the long edge to keep tokens/cost down.
    Returns a list of JPEG byte strings (empty if ffmpeg is unavailable or fails).
    """
    exe = ffmpeg_exe()
    if not exe:
        logger.info("No ffmpeg available; cannot extract GIF frames.")
        return []

    frames = []
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in.mp4")
        with open(inp, "wb") as f:
            f.write(data)

        dur = _probe_duration(exe, inp)
        if dur and dur > 0.1:
            if mode == "loop":
                # For GIFs and stickers, grab start, middle, and almost the end.
                fractions = [0.0, 0.5, 0.9]
                times = [dur * fr for fr in fractions]
            else:
                # For real videos, 1 frame per 3s evenly distributed, capped at max_frames
                interval = 3.0
                num_frames = min(max_frames, max(3, int(dur / interval)))
                if num_frames <= 1:
                    times = [dur * 0.5]
                else:
                    step = dur / num_frames
                    times = [step / 2 + i * step for i in range(num_frames)]
        else:
            times = [0.0]

        for i, t in enumerate(times):
            outp = os.path.join(d, f"frame_{i}.jpg")
            cmd = [
                exe, "-ss", f"{t:.3f}", "-i", inp,
                "-frames:v", "1",
                "-vf", f"scale='min({max_dim},iw)':-2",
                "-q:v", "4", "-y", outp,
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=20)
                if os.path.exists(outp) and os.path.getsize(outp) > 0:
                    with open(outp, "rb") as fr:
                        frames.append(fr.read())
            except Exception as e:
                logger.warning(f"ffmpeg frame extract failed at t={t:.2f}s: {e}")

    return frames


def extract_audio(data: bytes, max_dim: int = 0) -> bytes:
    """
    Extracts and normalizes audio from video, voice, or audio bytes.
    Converts to 32kbps mono mp3 to keep payload sizes small.
    Returns MP3 byte string (empty if unavailable or fails).
    """
    exe = ffmpeg_exe()
    if not exe:
        logger.info("No ffmpeg available; cannot extract audio.")
        return b""

    out_bytes = b""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in_media")
        with open(inp, "wb") as f:
            f.write(data)

        outp = os.path.join(d, "out.mp3")
        # -vn skips video, -ac 1 forces mono, -b:a 32k for low bitrate
        cmd = [
            exe, "-i", inp,
            "-vn", "-ac", "1", "-b:a", "32k",
            "-y", outp
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(outp) and os.path.getsize(outp) > 0:
                with open(outp, "rb") as fr:
                    out_bytes = fr.read()
        except Exception as e:
            logger.warning(f"ffmpeg audio extract failed: {e}")

    return out_bytes

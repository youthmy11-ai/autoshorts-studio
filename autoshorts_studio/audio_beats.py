"""Beat milestone parsing, clip-to-beat segmentation, and FFmpeg audio slicing."""

from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_SPLIT_RE = re.compile(r"[,;\r\n]+")

_CODEC_BY_EXT = {
    ".mp3": ("libmp3lame", "192k"),
    ".m4a": ("aac", "192k"),
    ".aac": ("aac", "192k"),
    ".wav": ("pcm_s16le", None),
    ".flac": ("flac", None),
    ".ogg": ("libvorbis", "192k"),
}


@dataclass(frozen=True)
class BeatSegment:
    """A single beat window optionally paired with a visual clip path."""

    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    clip_path: Path | None = None


def parse_timestamp(token: str | int | float) -> float:
    """Parse seconds, MM:SS(.ms), or HH:MM:SS(.ms) into non-negative float seconds."""
    if isinstance(token, bool):
        raise ValueError(f"Invalid timestamp boolean: {token!r}")

    if isinstance(token, (int, float)):
        val = float(token)
        if not math.isfinite(val) or val < 0:
            raise ValueError(f"Timestamp must be finite and non-negative, got {token!r}")
        return round(val, 3)

    text = str(token).strip()
    if not text:
        raise ValueError("Empty timestamp token.")

    if ":" not in text:
        try:
            val = float(text)
        except ValueError as exc:
            raise ValueError(f"Invalid numeric timestamp: {token!r}") from exc
        if not math.isfinite(val) or val < 0:
            raise ValueError(f"Timestamp must be finite and non-negative, got {token!r}")
        return round(val, 3)

    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid colon timestamp format: {token!r}")

    try:
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            hours = 0
        else:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp component in {token!r}") from exc

    if hours < 0 or minutes < 0 or seconds < 0:
        raise ValueError(f"Timestamp parts must be non-negative: {token!r}")
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Minutes and seconds must be < 60 in {token!r}")
    if not math.isfinite(seconds):
        raise ValueError(f"Non-finite timestamp seconds in {token!r}")

    total = hours * 3600 + minutes * 60 + seconds
    return round(total, 3)


def parse_milestones(
    raw: str | Sequence[str | int | float],
    mode: str = "timestamps",
) -> list[float]:
    """Parse milestone timestamps or cumulative durations into sorted unique seconds.

    Args:
        raw: Comma/semicolon/newline separated string or sequence of tokens.
        mode: "timestamps" (absolute points like "0:15, 0:30, 1:02") or
              "durations" / "cumulative" (segment lengths summed cumulatively).
    """
    normalized_mode = mode.strip().lower()
    if normalized_mode not in ("timestamps", "durations", "cumulative"):
        raise ValueError(f"Unsupported milestone mode: {mode!r}")

    if isinstance(raw, str):
        tokens = [part.strip() for part in _SPLIT_RE.split(raw) if part.strip()]
    else:
        tokens = [item for item in raw if str(item).strip() != ""]

    if not tokens:
        return []

    parsed = [parse_timestamp(item) for item in tokens]

    if normalized_mode in ("durations", "cumulative"):
        cumulative: list[float] = []
        running = 0.0
        for dur in parsed:
            if dur <= 0:
                continue
            running = round(running + dur, 3)
            cumulative.append(running)
        return cumulative

    # Timestamps mode: sort ascending and deduplicate identical timestamps.
    unique_sorted: list[float] = []
    for val in sorted(parsed):
        if not unique_sorted or abs(val - unique_sorted[-1]) > 1e-6:
            unique_sorted.append(val)
    return unique_sorted


def compute_beat_segments(
    milestones: Sequence[float],
    clip_paths: Sequence[Path | str] | None = None,
    total_duration: float | None = None,
    min_segment_seconds: float = 0.1,
    loop_clips: bool = True,
) -> list[BeatSegment]:
    """Convert milestone boundaries into non-zero segments paired with visual clips."""
    if min_segment_seconds < 0:
        raise ValueError("min_segment_seconds must be non-negative.")
    if total_duration is not None and total_duration <= 0:
        raise ValueError("total_duration must be positive when provided.")

    sorted_bounds = sorted(float(m) for m in milestones if float(m) >= 0.0)
    raw_windows: list[tuple[float, float]] = []
    cursor = 0.0

    for boundary in sorted_bounds:
        if boundary <= cursor:
            continue
        effective_end = boundary
        clipped = False
        if total_duration is not None and effective_end > total_duration:
            effective_end = float(total_duration)
            clipped = True
        duration = round(effective_end - cursor, 3)
        if duration >= min_segment_seconds and duration > 0:
            raw_windows.append((round(cursor, 3), round(effective_end, 3)))
            cursor = effective_end
        if clipped:
            break

    if total_duration is not None:
        tail_dur = round(float(total_duration) - cursor, 3)
        if tail_dur >= min_segment_seconds and tail_dur > 0:
            raw_windows.append((round(cursor, 3), round(float(total_duration), 3)))

    if not raw_windows:
        return []

    clips = [Path(p) for p in clip_paths] if clip_paths else []
    if clips and not loop_clips:
        raw_windows = raw_windows[: len(clips)]

    segments: list[BeatSegment] = []
    for idx, (start, end) in enumerate(raw_windows):
        paired_clip = clips[idx % len(clips)] if clips else None
        segments.append(
            BeatSegment(
                index=idx,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=round(end - start, 3),
                clip_path=paired_clip,
            )
        )
    return segments


def build_audio_trim_cmd(
    ffmpeg_bin: str,
    input_audio: Path | str,
    output_audio: Path | str,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    duration_seconds: float | None = None,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    reduce_seconds: float = 0.0,
    audio_codec: str | None = None,
    bitrate: str | None = None,
    sample_rate: int = 44100,
    loudnorm: bool = False,
) -> list[str]:
    """Construct an FFmpeg command for slicing and fading an audio milestone segment."""
    if start_seconds < 0 or reduce_seconds < 0 or fade_in < 0 or fade_out < 0:
        raise ValueError("Timing parameters (start, reduce, fade_in, fade_out) must be >= 0.")

    effective_start = 0.0 if start_seconds == 0 else max(0.0, float(start_seconds) - float(reduce_seconds))
    target_duration: float | None
    if end_seconds is not None:
        if end_seconds <= effective_start:
            raise ValueError(
                f"end_seconds ({end_seconds}) must exceed effective start ({effective_start})."
            )
        target_duration = round(float(end_seconds) - effective_start, 3)
    elif duration_seconds is not None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive.")
        target_duration = round(float(duration_seconds), 3)
    else:
        target_duration = None

    out_path = Path(output_audio)
    default_codec, default_bitrate = _CODEC_BY_EXT.get(out_path.suffix.lower(), ("aac", "192k"))
    codec = audio_codec or default_codec
    chosen_bitrate = bitrate if bitrate is not None else default_bitrate

    filters: list[str] = []
    if loudnorm:
        filters.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if fade_in > 0:
        eff_fade_in = min(fade_in, target_duration * 0.45) if target_duration else fade_in
        if eff_fade_in > 0:
            filters.append(f"afade=t=in:st=0:d={eff_fade_in:.3f}")
    if fade_out > 0 and target_duration is not None:
        eff_fade_out = min(fade_out, target_duration * 0.45)
        if eff_fade_out > 0:
            fade_start = max(0.0, target_duration - eff_fade_out)
            filters.append(f"afade=t=out:st={fade_start:.3f}:d={eff_fade_out:.3f}")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{effective_start:.3f}",
        "-i",
        str(input_audio),
    ]
    if target_duration is not None:
        cmd.extend(["-t", f"{target_duration:.3f}"])
    cmd.extend(["-vn", "-map", "0:a:0"])
    if filters:
        cmd.extend(["-af", ",".join(filters)])
    cmd.extend(["-c:a", codec])
    if chosen_bitrate and codec != "pcm_s16le":
        cmd.extend(["-b:a", chosen_bitrate])
    cmd.extend(["-ar", str(sample_rate), str(out_path)])
    return cmd


def slice_audio_by_milestones(
    ffmpeg_bin: str,
    input_audio: Path | str,
    output_dir: Path | str,
    milestones: str | Sequence[str | int | float],
    mode: str = "timestamps",
    total_duration: float | None = None,
    fade_in: float = 0.05,
    fade_out: float = 0.15,
    reduce_seconds: float = 0.0,
    output_ext: str = ".m4a",
    loudnorm: bool = False,
) -> list[Path]:
    """Slice an input audio file into numbered segment files based on beat milestones."""
    source = Path(input_audio)
    if not source.is_file():
        raise FileNotFoundError(f"Input audio not found: {source}")

    parsed = parse_milestones(milestones, mode=mode)
    segments = compute_beat_segments(parsed, total_duration=total_duration)
    if not segments:
        raise ValueError("No valid segments computed from the provided milestones.")

    ext = output_ext if output_ext.startswith(".") else f".{output_ext}"
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    for seg in segments:
        dest = out_root / f"{source.stem}_seg_{seg.index + 1:03d}{ext}"
        part = out_root / f".{source.stem}_seg_{seg.index + 1:03d}.part{ext}"
        cmd = build_audio_trim_cmd(
            ffmpeg_bin=ffmpeg_bin,
            input_audio=source,
            output_audio=part,
            start_seconds=seg.start_seconds,
            end_seconds=seg.end_seconds,
            fade_in=fade_in,
            fade_out=fade_out,
            reduce_seconds=reduce_seconds,
            loudnorm=loudnorm,
        )
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout).strip() or "FFmpeg audio slice failed")
            if not part.is_file() or part.stat().st_size == 0:
                raise RuntimeError(f"FFmpeg produced an empty audio segment: {dest.name}")
            os.replace(part, dest)
            outputs.append(dest)
        finally:
            part.unlink(missing_ok=True)
    return outputs

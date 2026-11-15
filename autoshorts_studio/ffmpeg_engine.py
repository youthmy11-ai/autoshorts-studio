"""FFmpeg resolution, aspect-ratio filtergraph builder, and batch video assembly engine."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .audio_beats import BeatSegment, compute_beat_segments, parse_milestones
from .dedup import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, MediaHistoryStore, detect_media_type
from .license import LicenseStatus, load_license_status

ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}

FIT_MODES = ("blur_pillarbox", "center_crop")

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class FFmpegNotFoundError(RuntimeError):
    """Raised when neither system PATH ffmpeg nor imageio-ffmpeg is available."""


class LicenseRequiredError(PermissionError):
    """Raised when a Pro-only feature or batch quota is requested on Community Edition."""


@dataclass(frozen=True)
class RenderResult:
    """Metadata describing a completed output video render."""

    output_path: Path
    preset: str
    fit_mode: str
    duration_seconds: float
    source_paths: tuple[Path, ...]
    output_sha256: str


@dataclass(frozen=True)
class MediaProbe:
    """Lightweight media inspection derived from FFmpeg stderr banner."""

    duration_seconds: float | None
    has_video: bool
    has_audio: bool


def resolve_ffmpeg_executable(explicit_path: str | None = None) -> str:
    """Resolve FFmpeg binary: explicit path -> system PATH -> imageio_ffmpeg fallback."""
    if explicit_path:
        candidate = shutil.which(explicit_path) or explicit_path
        if Path(candidate).is_file():
            return str(candidate)
        raise FFmpegNotFoundError(f"Configured FFmpeg executable not found: {explicit_path}")

    path_exe = shutil.which("ffmpeg")
    if path_exe:
        return path_exe

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except (ImportError, RuntimeError):
        pass

    raise FFmpegNotFoundError(
        "FFmpeg executable not found in PATH and imageio-ffmpeg fallback is unavailable. "
        "Install FFmpeg or run `pip install imageio-ffmpeg`."
    )


def probe_media(ffmpeg_bin: str, media_path: Path | str) -> MediaProbe:
    """Inspect duration and stream presence via FFmpeg without requiring ffprobe."""
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(media_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    info = proc.stderr or ""
    duration: float | None = None
    match = _DURATION_RE.search(info)
    if match:
        hours, mins, secs = int(match.group(1)), int(match.group(2)), float(match.group(3))
        duration = round(hours * 3600 + mins * 60 + secs, 3)

    has_video = " Video: " in info
    has_audio = " Audio: " in info
    return MediaProbe(duration_seconds=duration, has_video=has_video, has_audio=has_audio)


def _escape_drawtext(text: str) -> str:
    """Escape text safely for FFmpeg's drawtext filter expression."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def build_watermark_filter(watermark_text: str, width: int) -> str:
    """Create a subtle bottom-right drawtext watermark filter clause."""
    escaped = _escape_drawtext(watermark_text.strip())
    font_size = max(20, int(round(width * 0.028)))
    pad = max(20, int(round(width * 0.03)))
    return (
        f"drawtext=text='{escaped}':x=(w-text_w)-{pad}:y=(h-text_h)-{pad}:"
        f"fontsize={font_size}:fontcolor=white@0.86:shadowcolor=black@0.70:shadowx=2:shadowy=2"
    )


def build_video_filtergraph(
    preset: str = "9:16",
    fit_mode: str = "blur_pillarbox",
    is_image: bool = False,
    duration_seconds: float = 4.0,
    fps: int = 30,
    ken_burns: bool = True,
    watermark_text: str | None = None,
    input_label: str = "0:v",
    output_label: str = "vout",
    tag: str = "0",
    trim_video: bool = False,
    resolution: tuple[int, int] | None = None,
) -> str:
    """Build a deterministic FFmpeg video filtergraph for image or video inputs."""
    if resolution is not None:
        width, height = int(resolution[0]), int(resolution[1])
    else:
        if preset not in ASPECT_PRESETS:
            raise ValueError(f"Unsupported preset {preset!r}. Choose from {tuple(ASPECT_PRESETS)}.")
        width, height = ASPECT_PRESETS[preset]

    if fit_mode not in FIT_MODES:
        raise ValueError(f"Unsupported fit_mode {fit_mode!r}. Choose from {FIT_MODES}.")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive.")
    if fps < 1:
        raise ValueError("fps must be >= 1.")

    frames = max(1, int(round(duration_seconds * fps)))
    src = f"[{input_label}]"

    if is_image:
        if fit_mode == "blur_pillarbox":
            composed = (
                f"{src}split=2[bg_{tag}][fg_{tag}];"
                f"[bg_{tag}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=24,eq=brightness=-0.16,setsar=1[bgb_{tag}];"
                f"[fg_{tag}]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1[fgf_{tag}];"
                f"[bgb_{tag}][fgf_{tag}]overlay=(W-w)/2:(H-h)/2"
            )
            if ken_burns:
                chain = (
                    f"{composed},scale={width * 2}:{height * 2},setsar=1,"
                    f"zoompan=z='min(zoom+0.0012,1.12)':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={width}x{height}:fps={fps}"
                )
            else:
                chain = f"{composed},loop=loop={max(0, frames - 1)}:size=1:start=0,fps={fps}"
        else:
            if ken_burns:
                chain = (
                    f"{src}scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
                    f"crop={width * 2}:{height * 2},setsar=1,"
                    f"zoompan=z='min(zoom+0.0012,1.12)':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={frames}:s={width}x{height}:fps={fps}"
                )
            else:
                chain = (
                    f"{src}scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},setsar=1,"
                    f"loop=loop={max(0, frames - 1)}:size=1:start=0,fps={fps}"
                )
    else:
        pre = f"trim=duration={duration_seconds:.3f},setpts=PTS-STARTPTS," if trim_video else ""
        if fit_mode == "blur_pillarbox":
            chain = (
                f"{src}{pre}split=2[bg_{tag}][fg_{tag}];"
                f"[bg_{tag}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=24,eq=brightness=-0.16,setsar=1[bgb_{tag}];"
                f"[fg_{tag}]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1[fgf_{tag}];"
                f"[bgb_{tag}][fgf_{tag}]overlay=(W-w)/2:(H-h)/2:shortest=1,fps={fps}"
            )
        else:
            chain = (
                f"{src}{pre}scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps}"
            )

    if watermark_text and watermark_text.strip():
        chain = f"{chain},{build_watermark_filter(watermark_text, width)}"

    return f"{chain},format=yuv420p[{output_label}]"


def build_audio_filtergraph(
    duration_seconds: float,
    loudnorm: bool = True,
    fade_in: float = 0.2,
    fade_out: float = 0.4,
    input_label: str = "1:a",
    output_label: str = "aout",
) -> str:
    """Build an FFmpeg audio filtergraph with trim, fade-in/out, and optional loudnorm."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive.")

    stages = [f"atrim=0:{duration_seconds:.3f}", "asetpts=PTS-STARTPTS"]
    if loudnorm:
        stages.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if fade_in > 0:
        eff_in = min(fade_in, duration_seconds * 0.4)
        stages.append(f"afade=t=in:st=0:d={eff_in:.3f}")
    if fade_out > 0:
        eff_out = min(fade_out, duration_seconds * 0.4)
        fade_start = max(0.0, duration_seconds - eff_out)
        stages.append(f"afade=t=out:st={fade_start:.3f}:d={eff_out:.3f}")

    return f"[{input_label}]{','.join(stages)}[{output_label}]"


def build_render_command(
    ffmpeg_bin: str,
    segments: Sequence[tuple[Path, float, bool]],
    output_path: Path,
    preset: str = "9:16",
    fit_mode: str = "blur_pillarbox",
    fps: int = 30,
    ken_burns: bool = True,
    loudnorm: bool = True,
    soundtrack_path: Path | None = None,
    soundtrack_start: float = 0.0,
    use_first_source_audio: bool = False,
    watermark_text: str | None = None,
    crf: int = 22,
    video_preset: str = "veryfast",
    resolution: tuple[int, int] | None = None,
) -> list[str]:
    """Assemble a complete FFmpeg CLI invocation for single or multi-clip rendering."""
    if not segments:
        raise ValueError("At least one visual segment is required.")

    width, height = resolution if resolution is not None else ASPECT_PRESETS[preset]
    total_duration = round(sum(max(0.1, float(dur)) for _, dur, _ in segments), 3)

    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for src_path, dur, is_image in segments:
        if not is_image:
            cmd.extend(["-stream_loop", "-1", "-t", f"{dur:.3f}"])
        cmd.extend(["-i", str(src_path)])

    audio_input_idx: int | None = None
    if soundtrack_path is not None:
        audio_input_idx = len(segments)
        cmd.extend(["-stream_loop", "-1", "-ss", f"{max(0.0, soundtrack_start):.3f}", "-i", str(soundtrack_path)])
    elif use_first_source_audio:
        audio_input_idx = 0

    filter_parts: list[str] = []
    if len(segments) == 1:
        _, seg_dur, is_image = segments[0]
        filter_parts.append(
            build_video_filtergraph(
                preset=preset,
                fit_mode=fit_mode,
                is_image=is_image,
                duration_seconds=seg_dur,
                fps=fps,
                ken_burns=ken_burns,
                watermark_text=watermark_text,
                input_label="0:v",
                output_label="vout",
                tag="0",
                trim_video=not is_image,
                resolution=(width, height),
            )
        )
    else:
        concat_labels: list[str] = []
        for idx, (_, seg_dur, is_image) in enumerate(segments):
            v_lbl = f"vseg{idx}"
            concat_labels.append(f"[{v_lbl}]")
            filter_parts.append(
                build_video_filtergraph(
                    preset=preset,
                    fit_mode=fit_mode,
                    is_image=is_image,
                    duration_seconds=seg_dur,
                    fps=fps,
                    ken_burns=ken_burns,
                    watermark_text=None,
                    input_label=f"{idx}:v",
                    output_label=v_lbl,
                    tag=str(idx),
                    trim_video=not is_image,
                    resolution=(width, height),
                )
            )
        if watermark_text and watermark_text.strip():
            wm = build_watermark_filter(watermark_text, width)
            filter_parts.append(
                f"{''.join(concat_labels)}concat=n={len(segments)}:v=1:a=0,{wm},format=yuv420p[vout]"
            )
        else:
            filter_parts.append(
                f"{''.join(concat_labels)}concat=n={len(segments)}:v=1:a=0,format=yuv420p[vout]"
            )

    has_audio = audio_input_idx is not None
    if has_audio:
        filter_parts.append(
            build_audio_filtergraph(
                duration_seconds=total_duration,
                loudnorm=loudnorm,
                input_label=f"{audio_input_idx}:a",
                output_label="aout",
            )
        )

    cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[vout]"])
    if has_audio:
        cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "44100"])
    else:
        cmd.append("-an")

    cmd.extend(
        [
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            video_preset,
            "-crf",
            str(crf),
            "-movflags",
            "+faststart",
            "-max_muxing_queue_size",
            "1024",
            str(output_path),
        ]
    )
    return cmd


def _enforce_license_constraints(
    license_status: LicenseStatus,
    presets: Sequence[str],
    watermark_text: str | None,
    requested_count: int,
) -> None:
    if len(presets) > 1 and not license_status.allow_multi_preset:
        raise LicenseRequiredError(
            "Multi-preset simultaneous export requires AutoShorts Studio Pro. "
            f"Upgrade at {license_status.upgrade_url}"
        )
    if watermark_text and watermark_text.strip() and not license_status.allow_custom_watermark:
        raise LicenseRequiredError(
            "Custom watermark overlay requires AutoShorts Studio Pro. "
            f"Upgrade at {license_status.upgrade_url}"
        )
    if requested_count > license_status.max_batch_clips:
        raise LicenseRequiredError(
            f"Batch size {requested_count} exceeds the {license_status.tier} limit of "
            f"{license_status.max_batch_clips} outputs per run. Upgrade at {license_status.upgrade_url}"
        )


def collect_visual_sources(input_path: Path) -> list[Path]:
    """Collect supported video and image files from a file or directory."""
    if input_path.is_file():
        if detect_media_type(input_path) in ("video", "image"):
            return [input_path]
        raise ValueError(f"Unsupported visual file extension: {input_path.name}")
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    allowed = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    return sorted(
        p for p in input_path.rglob("*") if p.is_file() and p.suffix.lower() in allowed
    )


class AutoShortsEngine:
    """High-level batch video renderer combining deduplication, FFmpeg presets, and licensing."""

    def __init__(
        self,
        history_store: MediaHistoryStore | None = None,
        license_status: LicenseStatus | None = None,
        ffmpeg_path: str | None = None,
    ) -> None:
        self.history_store = history_store
        self.license_status = license_status or load_license_status()
        self.ffmpeg_bin = resolve_ffmpeg_executable(ffmpeg_path)

    def _run_ffmpeg_cmd(self, cmd: list[str], cancel: threading.Event | None = None) -> None:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise InterruptedError("Render cancelled by user.")
                proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass

        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                proc.wait(timeout=5)
                raise InterruptedError("Render cancelled by user.")
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue

        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg render failed: {stderr[-1500:].strip()}")

    def render_montage(
        self,
        media_items: Sequence[tuple[Path | str, float]],
        output_path: Path | str,
        preset: str = "9:16",
        fit_mode: str = "blur_pillarbox",
        fps: int = 30,
        ken_burns: bool = True,
        loudnorm: bool = True,
        soundtrack_path: Path | str | None = None,
        soundtrack_start: float = 0.0,
        watermark_text: str | None = None,
        record_history: bool = True,
        allow_source_reuse: bool = False,
        batch_id: str = "",
        cancel: threading.Event | None = None,
        resolution: tuple[int, int] | None = None,
    ) -> RenderResult:
        """Render a single output video from one or more (source_path, duration_seconds) items."""
        _enforce_license_constraints(
            self.license_status, [preset], watermark_text, requested_count=1
        )
        if not media_items:
            raise ValueError("media_items must contain at least one source clip or image.")

        prepared: list[tuple[Path, float, bool]] = []
        for raw_path, dur in media_items:
            src = Path(raw_path)
            if not src.is_file():
                raise FileNotFoundError(f"Source file not found: {src}")
            mtype = detect_media_type(src)
            if mtype not in ("video", "image"):
                raise ValueError(f"Unsupported media file: {src.name}")
            prepared.append((src, max(0.1, float(dur)), mtype == "image"))

        snd = Path(soundtrack_path) if soundtrack_path else None
        if snd is not None and not snd.is_file():
            raise FileNotFoundError(f"Soundtrack file not found: {snd}")

        use_first_audio = False
        if snd is None and len(prepared) == 1 and not prepared[0][2]:
            probe = probe_media(self.ffmpeg_bin, prepared[0][0])
            use_first_audio = probe.has_audio

        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        part_file = out_file.with_name(f".{out_file.stem}.{uuid.uuid4().hex[:8]}.part.mp4")

        cmd = build_render_command(
            ffmpeg_bin=self.ffmpeg_bin,
            segments=prepared,
            output_path=part_file,
            preset=preset,
            fit_mode=fit_mode,
            fps=fps,
            ken_burns=ken_burns,
            loudnorm=loudnorm,
            soundtrack_path=snd,
            soundtrack_start=soundtrack_start,
            use_first_source_audio=use_first_audio,
            watermark_text=watermark_text,
            resolution=resolution,
        )

        try:
            self._run_ffmpeg_cmd(cmd, cancel=cancel)
            if not part_file.is_file() or part_file.stat().st_size == 0:
                raise RuntimeError("FFmpeg produced an empty video file.")
            os.replace(part_file, out_file)
        finally:
            part_file.unlink(missing_ok=True)

        total_dur = round(sum(seg[1] for seg in prepared), 3)
        sources = tuple(seg[0] for seg in prepared)
        out_hash = ""
        if record_history and self.history_store is not None:
            out_hash = self.history_store.record_render(
                source_paths=sources,
                output_path=out_file,
                preset=preset,
                duration_seconds=total_dur,
                batch_id=batch_id,
                allow_reuse=allow_source_reuse,
            )

        return RenderResult(
            output_path=out_file,
            preset=preset,
            fit_mode=fit_mode,
            duration_seconds=total_dur,
            source_paths=sources,
            output_sha256=out_hash,
        )

    def run_batch(
        self,
        input_path: Path | str,
        output_dir: Path | str,
        presets: Sequence[str] = ("9:16",),
        fit_mode: str = "blur_pillarbox",
        soundtrack_path: Path | str | None = None,
        milestones: str | Sequence[str | int | float] | None = None,
        milestone_mode: str = "timestamps",
        clip_duration: float = 4.0,
        count: int = 1,
        montage_size: int = 1,
        fps: int = 30,
        ken_burns: bool = True,
        loudnorm: bool = True,
        watermark_text: str | None = None,
        log: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> list[RenderResult]:
        """Process fresh deduplicated sources into one or more preset exports."""
        preset_list = list(dict.fromkeys(presets))
        if not preset_list:
            raise ValueError("At least one aspect preset is required.")
        if count < 1:
            raise ValueError("count must be >= 1.")
        if montage_size < 1:
            raise ValueError("montage_size must be >= 1.")

        _enforce_license_constraints(
            self.license_status, preset_list, watermark_text, requested_count=count
        )

        logger = log or (lambda _msg: None)
        all_sources = collect_visual_sources(Path(input_path))
        if self.history_store is not None:
            fresh_pairs = self.history_store.filter_unprocessed(all_sources)
            fresh_sources = [pair[0] for pair in fresh_pairs]
        else:
            fresh_sources = all_sources

        if not fresh_sources:
            logger("No unprocessed media sources found after SHA-256 deduplication.")
            return []

        beat_segments: list[BeatSegment] = []
        if milestones:
            parsed = parse_milestones(milestones, mode=milestone_mode)
            beat_segments = compute_beat_segments(parsed)

        out_root = Path(output_dir)
        out_root.mkdir(parents=True, exist_ok=True)
        batch_id = uuid.uuid4().hex[:12]
        results: list[RenderResult] = []
        cursor = 0

        for job_idx in range(count):
            if cancel is not None and cancel.is_set():
                logger("Batch stopped on cancellation request.")
                break
            if cursor >= len(fresh_sources):
                logger("All fresh deduplicated sources have been consumed.")
                break

            if beat_segments:
                take = min(len(beat_segments), len(fresh_sources) - cursor)
                chosen = fresh_sources[cursor : cursor + take]
                cursor += take
                items = [
                    (chosen[idx], beat_segments[idx].duration_seconds)
                    for idx in range(len(chosen))
                ]
            else:
                take = min(montage_size, len(fresh_sources) - cursor)
                chosen = fresh_sources[cursor : cursor + take]
                cursor += take
                items = [(path, clip_duration) for path in chosen]

            for preset_idx, preset in enumerate(preset_list):
                slug = preset.replace(":", "x")
                out_name = f"autoshorts_{batch_id[:6]}_{job_idx + 1:03d}_{slug}.mp4"
                out_file = out_root / out_name
                logger(
                    f"[{job_idx + 1}/{count}] Rendering {out_name} "
                    f"({len(items)} clip(s), {preset}, {fit_mode})"
                )
                # Record source hashes on the first preset and allow recording additional
                # simultaneous aspect-ratio outputs for the same Pro batch selection.
                res = self.render_montage(
                    media_items=items,
                    output_path=out_file,
                    preset=preset,
                    fit_mode=fit_mode,
                    fps=fps,
                    ken_burns=ken_burns,
                    loudnorm=loudnorm,
                    soundtrack_path=soundtrack_path,
                    watermark_text=watermark_text,
                    record_history=True,
                    allow_source_reuse=(preset_idx > 0),
                    batch_id=batch_id,
                    cancel=cancel,
                )
                results.append(res)

        return results

"""AutoShorts Studio - Batch short-form video assembly & beat-milestone audio toolkit."""

from .audio_beats import (
    BeatSegment,
    build_audio_trim_cmd,
    compute_beat_segments,
    parse_milestones,
    parse_timestamp,
    slice_audio_by_milestones,
)
from .dedup import DuplicateMediaError, MediaHistoryStore, compute_file_sha256
from .ffmpeg_engine import (
    ASPECT_PRESETS,
    AutoShortsEngine,
    FFmpegNotFoundError,
    LicenseRequiredError,
    RenderResult,
    build_audio_filtergraph,
    build_render_command,
    build_video_filtergraph,
    resolve_ffmpeg_executable,
)
from .license import (
    PRICING_URL,
    LicenseStatus,
    clear_license_key,
    compute_license_checksum,
    format_license_key,
    load_license_status,
    save_license_key,
    validate_license_key,
)

__version__ = "1.0.0"

__all__ = [
    "ASPECT_PRESETS",
    "AutoShortsEngine",
    "BeatSegment",
    "DuplicateMediaError",
    "FFmpegNotFoundError",
    "LicenseRequiredError",
    "LicenseStatus",
    "MediaHistoryStore",
    "PRICING_URL",
    "RenderResult",
    "__version__",
    "build_audio_filtergraph",
    "build_audio_trim_cmd",
    "build_render_command",
    "build_video_filtergraph",
    "clear_license_key",
    "compute_beat_segments",
    "compute_file_sha256",
    "compute_license_checksum",
    "format_license_key",
    "load_license_status",
    "parse_milestones",
    "parse_timestamp",
    "resolve_ffmpeg_executable",
    "save_license_key",
    "slice_audio_by_milestones",
    "validate_license_key",
]

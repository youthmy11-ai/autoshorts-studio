"""Command-line interface for AutoShorts Studio."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audio_beats import compute_beat_segments, parse_milestones, slice_audio_by_milestones
from .dedup import MediaHistoryStore, compute_file_sha256
from .ffmpeg_engine import (
    ASPECT_PRESETS,
    FIT_MODES,
    AutoShortsEngine,
    collect_visual_sources,
    resolve_ffmpeg_executable,
)
from .license import (
    PRICING_URL,
    clear_license_key,
    default_config_dir,
    load_license_status,
    save_license_key,
    validate_license_key,
)


def _default_db_path() -> Path:
    return default_config_dir() / "media_history.sqlite3"


def _parse_presets(raw_presets: list[str] | None) -> list[str]:
    if not raw_presets:
        return ["9:16"]
    expanded: list[str] = []
    for item in raw_presets:
        for part in item.split(","):
            token = part.strip()
            if token:
                expanded.append(token)
    return expanded or ["9:16"]


def _cmd_build(args: argparse.Namespace) -> int:
    license_status = load_license_status(explicit_key=args.license_key)
    db_path = Path(args.db).expanduser() if args.db else _default_db_path()
    presets = _parse_presets(args.preset)

    with MediaHistoryStore(db_path) as store:
        engine = AutoShortsEngine(history_store=store, license_status=license_status)
        results = engine.run_batch(
            input_path=Path(args.input).expanduser(),
            output_dir=Path(args.output).expanduser(),
            presets=presets,
            fit_mode=args.fit,
            soundtrack_path=Path(args.audio).expanduser() if args.audio else None,
            milestones=args.milestones,
            milestone_mode=args.milestone_mode,
            clip_duration=args.duration,
            count=args.count,
            montage_size=args.montage_size,
            fps=args.fps,
            ken_burns=not args.no_ken_burns,
            loudnorm=not args.no_loudnorm,
            watermark_text=args.watermark,
            log=print,
        )

    if not results:
        print("No videos rendered (all matching sources were already processed).")
        return 0

    print(f"Completed {len(results)} export(s):")
    for item in results:
        print(f"  - {item.output_path} ({item.preset}, {item.duration_seconds:.2f}s)")
    return 0


def _cmd_slice_audio(args: argparse.Namespace) -> int:
    ffmpeg_bin = resolve_ffmpeg_executable()
    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output).expanduser()

    parsed = parse_milestones(args.milestones, mode=args.mode)
    segments = compute_beat_segments(parsed, total_duration=args.total_duration)
    if not segments:
        print("No segments found from the supplied milestones.", file=sys.stderr)
        return 1

    outputs = slice_audio_by_milestones(
        ffmpeg_bin=ffmpeg_bin,
        input_audio=input_path,
        output_dir=output_dir,
        milestones=args.milestones,
        mode=args.mode,
        total_duration=args.total_duration,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        reduce_seconds=args.reduce_seconds,
        output_ext=args.format,
        loudnorm=args.loudnorm,
    )
    print(f"Sliced {len(outputs)} audio segment(s) into {output_dir}:")
    for path in outputs:
        print(f"  - {path.name}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    db_path = Path(args.db).expanduser() if args.db else _default_db_path()
    sources = collect_visual_sources(input_path)

    seen_hashes: set[str] = set()
    in_batch_dupes = 0
    already_processed = 0
    fresh_count = 0

    with MediaHistoryStore(db_path) as store:
        for src in sources:
            digest = compute_file_sha256(src)
            if digest in seen_hashes:
                in_batch_dupes += 1
                continue
            seen_hashes.add(digest)
            if store.is_source_processed(digest):
                already_processed += 1
            else:
                fresh_count += 1
        stats = store.stats()

    print(f"Scanned folder: {input_path}")
    print(f"History DB:     {db_path}")
    print(f"Total files:    {len(sources)}")
    print(f"Fresh unique:   {fresh_count}")
    print(f"Already used:   {already_processed}")
    print(f"In-folder dupes:{in_batch_dupes}")
    print(f"DB totals:      {stats['processed_sources']} sources / {stats['rendered_outputs']} outputs")
    return 0


def _cmd_check_license(args: argparse.Namespace) -> int:
    if args.clear:
        clear_license_key()
        print("Saved Pro license key removed. Community Edition is now active.")
        return 0

    if args.activate:
        status = save_license_key(args.activate)
        print(f"Activated: {status.reason}")
        print(f"Tier:      {status.tier.upper()}")
        print(f"Key:       {status.masked_key}")
        return 0

    status = validate_license_key(args.key) if args.key else load_license_status()
    print(f"Tier:             {status.tier.upper()}")
    print(f"Status:           {status.reason}")
    print(f"Max batch clips:  {status.max_batch_clips}")
    print(f"Multi-preset:     {'Unlocked' if status.allow_multi_preset else 'Pro only'}")
    print(f"Custom watermark: {'Unlocked' if status.allow_custom_watermark else 'Pro only'}")
    if not status.is_pro:
        print(f"Upgrade to Pro:   {PRICING_URL}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoshorts-studio",
        description="AutoShorts Studio - Deduplicated short-form video assembly & beat slicer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Render deduplicated short-form videos.")
    p_build.add_argument("--input", required=True, help="Input media file or folder.")
    p_build.add_argument("--output", required=True, help="Output folder for rendered MP4s.")
    p_build.add_argument("--audio", help="Optional soundtrack audio file.")
    p_build.add_argument(
        "--preset",
        action="append",
        help=f"Aspect preset ({', '.join(ASPECT_PRESETS)}). Repeat or comma-separate for Pro multi-export.",
    )
    p_build.add_argument("--fit", default="blur_pillarbox", choices=FIT_MODES, help="Framing mode.")
    p_build.add_argument("--milestones", help="Optional comma-separated beat milestones (e.g. '0:03, 0:06').")
    p_build.add_argument(
        "--milestone-mode",
        default="timestamps",
        choices=("timestamps", "durations"),
        help="Interpret milestones as timeline timestamps or cumulative durations.",
    )
    p_build.add_argument("--duration", type=float, default=4.0, help="Seconds per clip when milestones omitted.")
    p_build.add_argument("--count", type=int, default=1, help="Number of batch outputs to produce.")
    p_build.add_argument("--montage-size", type=int, default=1, help="Clips combined per video when milestones omitted.")
    p_build.add_argument("--fps", type=int, default=30, help="Output frame rate.")
    p_build.add_argument("--no-ken-burns", action="store_true", help="Disable zoom-pan on static images.")
    p_build.add_argument("--no-loudnorm", action="store_true", help="Disable EBU R128 loudness normalization.")
    p_build.add_argument("--watermark", help="Custom watermark overlay text (Pro Edition).")
    p_build.add_argument("--license-key", help="Optional Pro license key override.")
    p_build.add_argument("--db", help="Optional SQLite deduplication database path.")
    p_build.set_defaults(func=_cmd_build)

    p_slice = sub.add_parser("slice-audio", help="Slice an audio track at beat milestones.")
    p_slice.add_argument("--input", required=True, help="Input audio track.")
    p_slice.add_argument("--output", required=True, help="Output directory for sliced segments.")
    p_slice.add_argument("--milestones", required=True, help="Comma-separated timestamps or durations.")
    p_slice.add_argument(
        "--mode",
        default="timestamps",
        choices=("timestamps", "durations"),
        help="Milestone interpretation mode.",
    )
    p_slice.add_argument("--total-duration", type=float, help="Optional total track duration for trailing segment.")
    p_slice.add_argument("--fade-in", type=float, default=0.05, help="Fade-in duration in seconds.")
    p_slice.add_argument("--fade-out", type=float, default=0.15, help="Fade-out duration in seconds.")
    p_slice.add_argument("--reduce-seconds", type=float, default=0.0, help="Pre-roll lead offset before start marker.")
    p_slice.add_argument("--format", default=".m4a", choices=(".m4a", ".mp3", ".wav"), help="Output audio format.")
    p_slice.add_argument("--loudnorm", action="store_true", help="Normalize output segment loudness.")
    p_slice.set_defaults(func=_cmd_slice_audio)

    p_scan = sub.add_parser("scan", help="Scan a media folder and report SHA-256 deduplication counts.")
    p_scan.add_argument("--input", required=True, help="Folder of images/videos to inspect.")
    p_scan.add_argument("--db", help="Optional SQLite deduplication database path.")
    p_scan.set_defaults(func=_cmd_scan)

    p_lic = sub.add_parser("check-license", help="Inspect, activate, or clear a Pro license key.")
    p_lic.add_argument("--key", help="Check a specific license key without saving it.")
    p_lic.add_argument("--activate", help="Validate and save a Pro license key.")
    p_lic.add_argument("--clear", action="store_true", help="Clear any locally saved Pro license key.")
    p_lic.set_defaults(func=_cmd_check_license)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

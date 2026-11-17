"""Comprehensive unit and end-to-end FFmpeg tests for AutoShorts Studio."""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from autoshorts_studio.audio_beats import (
    build_audio_trim_cmd,
    build_beat_srt,
    compute_beat_segments,
    format_srt_timestamp,
    parse_milestones,
    parse_timestamp,
    slice_audio_by_milestones,
)
from autoshorts_studio.cli import main as cli_main
from autoshorts_studio.dedup import DuplicateMediaError, MediaHistoryStore, compute_file_sha256
from autoshorts_studio.ffmpeg_engine import (
    AutoShortsEngine,
    LicenseRequiredError,
    build_audio_filtergraph,
    build_video_filtergraph,
    probe_media,
    resolve_ffmpeg_executable,
)
from autoshorts_studio.license import (
    COMMUNITY_MAX_BATCH_CLIPS,
    PRO_MAX_BATCH_CLIPS,
    format_license_key,
    load_license_status,
    save_license_key,
    validate_license_key,
)


class DeduplicationTests(unittest.TestCase):
    def test_sha256_dedup_persists_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="autoshorts_dedup_") as tmp:
            root = Path(tmp)
            db_path = root / "history.sqlite3"
            src_a = root / "clip_a.mp4"
            src_a_copy = root / "clip_a_renamed.mp4"
            src_b = root / "clip_b.mp4"
            out_1 = root / "out_001.mp4"

            src_a.write_bytes(b"video-payload-alpha-123456")
            src_a_copy.write_bytes(b"video-payload-alpha-123456")
            src_b.write_bytes(b"video-payload-beta-7890")
            out_1.write_bytes(b"rendered-output-mp4")

            self.assertEqual(compute_file_sha256(src_a), compute_file_sha256(src_a_copy))
            self.assertNotEqual(compute_file_sha256(src_a), compute_file_sha256(src_b))

            with MediaHistoryStore(db_path) as store:
                journal_mode = store._conn.execute("PRAGMA journal_mode;").fetchone()[0]
                self.assertEqual(journal_mode.lower(), "wal")

                fresh_before = store.filter_unprocessed([src_a, src_a_copy, src_b])
                self.assertEqual([item[0] for item in fresh_before], [src_a, src_b])

                store.record_render(
                    source_paths=[src_a],
                    output_path=out_1,
                    preset="9:16",
                    duration_seconds=3.0,
                )

            # Re-open SQLite DB to verify persistence across restarts.
            with MediaHistoryStore(db_path) as reopened:
                self.assertTrue(reopened.is_path_processed(src_a))
                self.assertTrue(reopened.is_path_processed(src_a_copy))
                self.assertFalse(reopened.is_path_processed(src_b))

                remaining = reopened.filter_unprocessed([src_a, src_a_copy, src_b])
                self.assertEqual([item[0] for item in remaining], [src_b])

                with self.assertRaises(DuplicateMediaError):
                    reopened.record_render(
                        source_paths=[src_a_copy],
                        output_path=root / "out_002.mp4",
                        preset="9:16",
                    )


class AudioBeatTests(unittest.TestCase):
    def test_parse_milestones_timestamps_and_edge_cases(self) -> None:
        self.assertEqual(parse_milestones(""), [])
        self.assertEqual(parse_milestones("0:30, 0:15, 1:02, 0:15, 0:00"), [0.0, 15.0, 30.0, 62.0])
        self.assertEqual(parse_milestones(["00:01:05.5", "10", "1:05.5"]), [10.0, 65.5])

        with self.assertRaises(ValueError):
            parse_timestamp("1:75")
        with self.assertRaises(ValueError):
            parse_timestamp("-5")

    def test_parse_milestones_cumulative_durations(self) -> None:
        parsed = parse_milestones("5, 0, 10.5, 4.5", mode="durations")
        self.assertEqual(parsed, [5.0, 15.5, 20.0])

    def test_compute_beat_segments_zero_and_clip_pairing(self) -> None:
        # Leading zero should never emit a zero-length segment.
        milestones = parse_milestones("0, 0:05, 0:12, 0:20")
        clips = [Path("a.jpg"), Path("b.jpg")]

        segs_loop = compute_beat_segments(milestones, clip_paths=clips, loop_clips=True)
        self.assertEqual(len(segs_loop), 3)
        self.assertEqual(
            [(s.start_seconds, s.end_seconds, s.duration_seconds, s.clip_path) for s in segs_loop],
            [
                (0.0, 5.0, 5.0, Path("a.jpg")),
                (5.0, 12.0, 7.0, Path("b.jpg")),
                (12.0, 20.0, 8.0, Path("a.jpg")),
            ],
        )

        # Clamping to total_duration adds trailing tail or trims overshoot cleanly.
        segs_clamped = compute_beat_segments(
            [4.0, 9.0, 25.0],
            clip_paths=clips,
            total_duration=11.5,
            loop_clips=True,
        )
        self.assertEqual(
            [(s.start_seconds, s.end_seconds) for s in segs_clamped],
            [(0.0, 4.0), (4.0, 9.0), (9.0, 11.5)],
        )

        # Zero-only milestone with no total_duration yields empty segments safely.
        self.assertEqual(compute_beat_segments([0.0]), [])

    def test_build_audio_trim_cmd_with_fades_and_reduce(self) -> None:
        cmd = build_audio_trim_cmd(
            ffmpeg_bin="ffmpeg",
            input_audio="track.mp3",
            output_audio="out.m4a",
            start_seconds=16.0,
            end_seconds=26.0,
            fade_in=0.2,
            fade_out=0.5,
            reduce_seconds=1.0,
            loudnorm=True,
        )
        joined = " ".join(cmd)
        self.assertIn("-ss 15.000", joined)
        self.assertIn("-t 11.000", joined)
        self.assertIn("loudnorm=I=-14:TP=-1.5:LRA=11", joined)
        self.assertIn("afade=t=in:st=0:d=0.200", joined)
        self.assertIn("afade=t=out:st=10.500:d=0.500", joined)


class FiltergraphAndLicenseTests(unittest.TestCase):
    def test_filtergraph_presets_and_ken_burns(self) -> None:
        fg_9_16 = build_video_filtergraph(preset="9:16", fit_mode="blur_pillarbox", is_image=False)
        self.assertIn("scale=1080:1920", fg_9_16)
        self.assertIn("gblur=sigma=24", fg_9_16)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fg_9_16)

        fg_16_9 = build_video_filtergraph(preset="16:9", fit_mode="center_crop", is_image=False)
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080", fg_16_9)

        fg_1_1_kb = build_video_filtergraph(
            preset="1:1",
            fit_mode="center_crop",
            is_image=True,
            duration_seconds=3.0,
            fps=30,
            ken_burns=True,
            watermark_text="Brand Studio",
        )
        self.assertIn("zoompan=z='min(zoom+0.0012,1.12)'", fg_1_1_kb)
        self.assertIn("d=90:s=1080x1080:fps=30", fg_1_1_kb)
        self.assertIn("drawtext=text='Brand Studio'", fg_1_1_kb)

        af = build_audio_filtergraph(duration_seconds=5.0, loudnorm=True, fade_in=0.25, fade_out=0.5)
        self.assertIn("loudnorm=I=-14", af)
        self.assertIn("afade=t=out:st=4.500:d=0.500", af)

    def test_license_checksum_validation_and_tier_gating(self) -> None:
        as_key = format_license_key("AS", "A1B2C3D4")
        bundle_key = format_license_key("BUNDLE", "99FF00AA")
        wl_key = format_license_key("WL", "A1B2C3D4")

        as_status = validate_license_key(as_key)
        self.assertTrue(as_status.is_pro)
        self.assertEqual(as_status.tier, "pro")
        self.assertEqual(as_status.product_code, "AS")
        self.assertEqual(as_status.max_batch_clips, PRO_MAX_BATCH_CLIPS)
        self.assertTrue(as_status.allow_custom_watermark)
        self.assertTrue(as_status.allow_multi_preset)

        bundle_status = validate_license_key(bundle_key.lower())
        self.assertTrue(bundle_status.is_pro)
        self.assertEqual(bundle_status.product_code, "BUNDLE")

        # Cross-product key, bad checksum, and zero payload must remain Community.
        self.assertFalse(validate_license_key(wl_key).is_pro)
        self.assertFalse(validate_license_key(as_key[:-1] + ("0" if as_key[-1] != "0" else "1")).is_pro)
        self.assertFalse(validate_license_key(format_license_key("AS", "00000000")).is_pro)
        self.assertFalse(validate_license_key("").is_pro)

        community = validate_license_key(None)
        self.assertEqual(community.max_batch_clips, COMMUNITY_MAX_BATCH_CLIPS)
        self.assertFalse(community.allow_custom_watermark)

        with tempfile.TemporaryDirectory(prefix="autoshorts_lic_") as tmp:
            cfg = Path(tmp)
            save_license_key(as_key, config_dir=cfg)
            loaded = load_license_status(config_dir=cfg)
            self.assertTrue(loaded.is_pro)

        # Engine gating checks
        engine = AutoShortsEngine(license_status=community, ffmpeg_path=resolve_ffmpeg_executable())
        with self.assertRaises(LicenseRequiredError):
            engine.render_montage(
                media_items=[("missing.mp4", 1.0)],
                output_path="out.mp4",
                watermark_text="ProOnly",
            )
        with self.assertRaises(LicenseRequiredError):
            engine.run_batch(
                input_path="missing_dir",
                output_dir="out_dir",
                presets=["9:16", "16:9"],
                count=1,
            )
        with self.assertRaises(LicenseRequiredError):
            engine.run_batch(
                input_path="missing_dir",
                output_dir="out_dir",
                presets=["9:16"],
                count=6,
            )

    def test_gui_import_has_no_side_effects(self) -> None:
        mod = importlib.import_module("autoshorts_studio.gui")
        self.assertTrue(callable(mod.launch_gui))


class RealFFmpegEndToEndTests(unittest.TestCase):
    def test_end_to_end_lavfi_video_image_and_audio_slice(self) -> None:
        ffmpeg_bin = resolve_ffmpeg_executable()
        self.assertTrue(Path(ffmpeg_bin).is_file())

        with tempfile.TemporaryDirectory(prefix="autoshorts_e2e_") as tmp:
            root = Path(tmp)
            media_dir = root / "media"
            out_dir = root / "outputs"
            slice_dir = root / "slices"
            db_path = root / "studio_history.sqlite3"
            media_dir.mkdir(parents=True)

            src_video = media_dir / "clip_01.mp4"
            src_image = media_dir / "still_02.png"
            src_audio = root / "beat_track.wav"

            # Generate synthetic testsrc video (640x360, 1.5s) and sine wave audio (3.0s).
            subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=640x360:rate=30:duration=1.5",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(src_video),
                ],
                check=True,
            )
            subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=640x480:duration=0.1",
                    "-frames:v",
                    "1",
                    str(src_image),
                ],
                check=True,
            )
            subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=3.0",
                    "-ar",
                    "44100",
                    str(src_audio),
                ],
                check=True,
            )

            # 1. Slice audio at beat milestones
            sliced = slice_audio_by_milestones(
                ffmpeg_bin=ffmpeg_bin,
                input_audio=src_audio,
                output_dir=slice_dir,
                milestones="0:01, 0:02",
                total_duration=3.0,
                output_ext=".m4a",
            )
            self.assertEqual(len(sliced), 3)
            for audio_seg in sliced:
                self.assertTrue(audio_seg.is_file())
                self.assertGreater(audio_seg.stat().st_size, 0)

            # 2. Render Pro multi-preset batch combining video + Ken Burns image + sine audio + watermark
            pro_key = format_license_key("AS", "TESTPRO1")
            pro_status = validate_license_key(pro_key)
            with MediaHistoryStore(db_path) as store:
                engine = AutoShortsEngine(history_store=store, license_status=pro_status)
                results = engine.run_batch(
                    input_path=media_dir,
                    output_dir=out_dir,
                    presets=["9:16", "1:1"],
                    fit_mode="blur_pillarbox",
                    soundtrack_path=src_audio,
                    clip_duration=1.0,
                    count=1,
                    montage_size=2,
                    fps=24,
                    ken_burns=True,
                    loudnorm=True,
                    watermark_text="AutoShorts Pro",
                )
                self.assertEqual(len(results), 2)
                for res in results:
                    self.assertTrue(res.output_path.is_file())
                    self.assertGreater(res.output_path.stat().st_size, 0)
                    probed = probe_media(ffmpeg_bin, res.output_path)
                    self.assertTrue(probed.has_video)
                    self.assertTrue(probed.has_audio)

                # Subsequent batch on the exact same folder must find 0 unprocessed sources.
                second_run = engine.run_batch(
                    input_path=media_dir,
                    output_dir=out_dir,
                    presets=["9:16"],
                    count=1,
                )
                self.assertEqual(second_run, [])
                self.assertEqual(store.stats(), {"processed_sources": 2, "rendered_outputs": 2})

            # 3. Verify CLI `check-license`, `scan`, `slice-audio`, and `build` execution
            cli_out = io.StringIO()
            os.environ["AUTOSHORTS_CONFIG_DIR"] = str(root / "cfg")
            try:
                with contextlib.redirect_stdout(cli_out):
                    self.assertEqual(cli_main(["check-license", "--key", pro_key]), 0)
                    self.assertEqual(cli_main(["scan", "--input", str(media_dir), "--db", str(db_path)]), 0)
                    self.assertEqual(
                        cli_main(
                            [
                                "slice-audio",
                                "--input",
                                str(src_audio),
                                "--output",
                                str(root / "cli_slices"),
                                "--milestones",
                                "1.5",
                                "--total-duration",
                                "3.0",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        cli_main(
                            [
                                "subtitles-srt",
                                "--milestones",
                                "1.5",
                                "--total-duration",
                                "3.0",
                                "--captions",
                                "Hook Intro | Call To Action",
                                "--output",
                                str(root / "subs" / "beat.srt"),
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        cli_main(
                            [
                                "build",
                                "--input",
                                str(src_image),
                                "--audio",
                                str(src_audio),
                                "--output",
                                str(root / "cli_build"),
                                "--preset",
                                "16:9",
                                "--fit",
                                "center_crop",
                                "--duration",
                                "1.0",
                                "--db",
                                str(root / "cli_db.sqlite3"),
                            ]
                        ),
                        0,
                    )
                cli_text = cli_out.getvalue()
                self.assertIn("Tier:             PRO", cli_text)
                self.assertIn("Fresh unique:   0", cli_text)
                self.assertIn("2 sources / 2 outputs", cli_text)
                srt_text = (root / "subs" / "beat.srt").read_text(encoding="utf-8")
                self.assertEqual(format_srt_timestamp(3661.25), "01:01:01,250")
                self.assertIn("00:00:00,000 --> 00:00:01,500\nHook Intro", srt_text)
                self.assertIn("00:00:01,500 --> 00:00:03,000\nCall To Action", srt_text)
                direct_srt = build_beat_srt("0:02, 0:05", total_duration=7.0)
                self.assertIn("Scene 1", direct_srt)
                self.assertIn("Scene 3", direct_srt)
                cli_mp4s = list((root / "cli_build").glob("*.mp4"))
                self.assertEqual(len(cli_mp4s), 1)
                self.assertGreater(cli_mp4s[0].stat().st_size, 0)
            finally:
                os.environ.pop("AUTOSHORTS_CONFIG_DIR", None)


if __name__ == "__main__":
    unittest.main()

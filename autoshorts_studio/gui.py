"""Dark-slate desktop Tkinter interface for AutoShorts Studio."""

from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path

from .audio_beats import slice_audio_by_milestones
from .dedup import MediaHistoryStore
from .ffmpeg_engine import (
    ASPECT_PRESETS,
    FIT_MODES,
    AutoShortsEngine,
    collect_visual_sources,
    resolve_ffmpeg_executable,
)
from .license import (
    PRICING_URL,
    LicenseStatus,
    clear_license_key,
    default_config_dir,
    load_license_status,
    save_license_key,
)


def _default_db_path() -> Path:
    return default_config_dir() / "media_history.sqlite3"


def launch_gui() -> None:
    """Create and run the AutoShorts Studio desktop window."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    bg_main = "#0F172A"
    bg_card = "#1E293B"
    fg_text = "#F8FAFC"
    fg_muted = "#94A3B8"
    accent = "#38BDF8"

    app = tk.Tk()
    app.title("AutoShorts Studio v1.0")
    app.geometry("940x680")
    app.minsize(820, 600)
    app.configure(bg=bg_main)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("TFrame", background=bg_main)
    style.configure("Card.TFrame", background=bg_card)
    style.configure("TLabel", background=bg_main, foreground=fg_text, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=bg_main, foreground=fg_muted, font=("Segoe UI", 9))
    style.configure("Header.TLabel", background=bg_main, foreground=fg_text, font=("Segoe UI", 16, "bold"))
    style.configure("Accent.TLabel", background=bg_main, foreground=accent, font=("Segoe UI", 10, "bold"))
    style.configure("TNotebook", background=bg_main, borderwidth=0)
    style.configure("TNotebook.Tab", background=bg_card, foreground=fg_muted, padding=(14, 7))
    style.map("TNotebook.Tab", background=[("selected", "#334155")], foreground=[("selected", fg_text)])
    style.configure("TButton", background=bg_card, foreground=fg_text, padding=(10, 6))
    style.map("TButton", background=[("active", "#334155")])
    style.configure("Primary.TButton", background="#0284C7", foreground="#FFFFFF", padding=(12, 7))
    style.map("Primary.TButton", background=[("active", "#0369A1")])
    style.configure("TCheckbutton", background=bg_main, foreground=fg_text)
    style.configure("TEntry", fieldbackground=bg_card, foreground=fg_text, insertcolor=fg_text)
    style.configure("TCombobox", fieldbackground=bg_card, foreground=fg_text)

    log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    cancel_event = threading.Event()
    busy_state = {"running": False}

    # State variables
    input_var = tk.StringVar()
    audio_var = tk.StringVar()
    output_var = tk.StringVar()
    db_var = tk.StringVar(value=str(_default_db_path()))
    fit_var = tk.StringVar(value="blur_pillarbox")
    duration_var = tk.StringVar(value="4.0")
    count_var = tk.StringVar(value="3")
    montage_var = tk.StringVar(value="1")
    milestones_var = tk.StringVar()
    watermark_var = tk.StringVar()
    ken_burns_var = tk.BooleanVar(value=True)
    loudnorm_var = tk.BooleanVar(value=True)
    preset_vars = {p: tk.BooleanVar(value=(p == "9:16")) for p in ASPECT_PRESETS}

    slice_input_var = tk.StringVar()
    slice_output_var = tk.StringVar()
    slice_marks_var = tk.StringVar(value="0:15, 0:30, 1:02")
    slice_mode_var = tk.StringVar(value="timestamps")
    slice_ext_var = tk.StringVar(value=".m4a")
    slice_fade_in_var = tk.StringVar(value="0.05")
    slice_fade_out_var = tk.StringVar(value="0.15")

    license_entry_var = tk.StringVar()
    tier_banner_var = tk.StringVar()
    dedup_stats_var = tk.StringVar(value="History not scanned yet.")

    outer = ttk.Frame(app, padding=16)
    outer.pack(fill="both", expand=True)

    top_bar = ttk.Frame(outer)
    top_bar.pack(fill="x", pady=(0, 10))
    ttk.Label(top_bar, text="AutoShorts Studio", style="Header.TLabel").pack(side="left")
    ttk.Label(top_bar, textvariable=tier_banner_var, style="Accent.TLabel").pack(side="right")

    notebook = ttk.Notebook(outer)
    notebook.pack(fill="both", expand=True)

    tab_video = ttk.Frame(notebook, padding=12)
    tab_beats = ttk.Frame(notebook, padding=12)
    tab_license = ttk.Frame(notebook, padding=12)
    notebook.add(tab_video, text="Video Studio")
    notebook.add(tab_beats, text="Beat Slicer")
    notebook.add(tab_license, text="Dedup & License")

    def refresh_license_banner() -> LicenseStatus:
        status = load_license_status()
        label = (
            f"PRO ACTIVE ({status.product_code} | up to {status.max_batch_clips} outputs)"
            if status.is_pro
            else f"COMMUNITY EDITION (up to {status.max_batch_clips} outputs/batch)"
        )
        tier_banner_var.set(label)
        return status

    def add_path_row(parent: ttk.Frame, label: str, var: tk.StringVar, dir_mode: bool = True) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=20).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 8))

        def pick() -> None:
            chosen = filedialog.askdirectory(parent=app) if dir_mode else filedialog.askopenfilename(parent=app)
            if chosen:
                var.set(chosen)

        ttk.Button(row, text="Browse...", command=pick).pack(side="left")

    # --- Tab 1: Video Studio ---
    add_path_row(tab_video, "Source Media Folder:", input_var, dir_mode=True)
    add_path_row(tab_video, "Soundtrack (optional):", audio_var, dir_mode=False)
    add_path_row(tab_video, "Output Folder:", output_var, dir_mode=True)

    preset_row = ttk.Frame(tab_video)
    preset_row.pack(fill="x", pady=6)
    ttk.Label(preset_row, text="Aspect Presets:", width=20).pack(side="left")
    for p_name, p_var in preset_vars.items():
        ttk.Checkbutton(preset_row, text=p_name, variable=p_var).pack(side="left", padx=(0, 12))
    ttk.Label(preset_row, text="Framing:").pack(side="left", padx=(12, 6))
    ttk.Combobox(preset_row, textvariable=fit_var, state="readonly", values=FIT_MODES, width=16).pack(side="left")

    opts_row = ttk.Frame(tab_video)
    opts_row.pack(fill="x", pady=6)
    ttk.Label(opts_row, text="Clip Seconds:").pack(side="left")
    ttk.Entry(opts_row, textvariable=duration_var, width=6).pack(side="left", padx=(6, 14))
    ttk.Label(opts_row, text="Outputs:").pack(side="left")
    ttk.Entry(opts_row, textvariable=count_var, width=6).pack(side="left", padx=(6, 14))
    ttk.Label(opts_row, text="Clips/Video:").pack(side="left")
    ttk.Entry(opts_row, textvariable=montage_var, width=6).pack(side="left", padx=(6, 14))
    ttk.Checkbutton(opts_row, text="Ken Burns Zoom", variable=ken_burns_var).pack(side="left", padx=8)
    ttk.Checkbutton(opts_row, text="Loudness Norm (-14 LUFS)", variable=loudnorm_var).pack(side="left")

    pro_row = ttk.Frame(tab_video)
    pro_row.pack(fill="x", pady=4)
    ttk.Label(pro_row, text="Beat Milestones:", width=20).pack(side="left")
    ttk.Entry(pro_row, textvariable=milestones_var).pack(side="left", fill="x", expand=True, padx=(0, 12))
    ttk.Label(pro_row, text="Pro Watermark:").pack(side="left")
    ttk.Entry(pro_row, textvariable=watermark_var, width=22).pack(side="left", padx=(6, 0))

    btn_row = ttk.Frame(tab_video)
    btn_row.pack(fill="x", pady=10)

    def append_log(line: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", f"{line}\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def start_batch() -> None:
        if busy_state["running"]:
            return
        src = input_var.get().strip()
        out = output_var.get().strip()
        if not src or not out:
            messagebox.showerror("Missing Paths", "Please choose both Source Media and Output folders.", parent=app)
            return
        selected_presets = [name for name, var in preset_vars.items() if var.get()]
        if not selected_presets:
            messagebox.showerror("Missing Preset", "Select at least one aspect ratio preset.", parent=app)
            return
        try:
            clip_dur = float(duration_var.get())
            batch_count = int(count_var.get())
            montage = int(montage_var.get())
        except ValueError:
            messagebox.showerror("Invalid Number", "Duration, Outputs, and Clips/Video must be valid numbers.", parent=app)
            return

        cancel_event.clear()
        busy_state["running"] = True
        run_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        append_log("Starting batch render...")

        def worker() -> None:
            try:
                status = load_license_status()
                with MediaHistoryStore(Path(db_var.get().strip())) as store:
                    engine = AutoShortsEngine(history_store=store, license_status=status)
                    res = engine.run_batch(
                        input_path=Path(src),
                        output_dir=Path(out),
                        presets=selected_presets,
                        fit_mode=fit_var.get(),
                        soundtrack_path=Path(audio_var.get().strip()) if audio_var.get().strip() else None,
                        milestones=milestones_var.get().strip() or None,
                        clip_duration=clip_dur,
                        count=batch_count,
                        montage_size=montage,
                        ken_burns=ken_burns_var.get(),
                        loudnorm=loudnorm_var.get(),
                        watermark_text=watermark_var.get().strip() or None,
                        log=lambda msg: log_queue.put(("log", msg)),
                        cancel=cancel_event,
                    )
                log_queue.put(("done", f"Completed {len(res)} export(s)."))
            except Exception as exc:
                log_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    run_btn = ttk.Button(btn_row, text="Render Batch", style="Primary.TButton", command=start_batch)
    run_btn.pack(side="left")
    stop_btn = ttk.Button(btn_row, text="Stop", command=cancel_event.set, state="disabled")
    stop_btn.pack(side="left", padx=8)

    log_box = tk.Text(
        tab_video,
        bg="#020617",
        fg="#E2E8F0",
        insertbackground="#FFFFFF",
        relief="flat",
        font=("Consolas", 9),
        padx=10,
        pady=8,
    )
    log_box.pack(fill="both", expand=True)
    log_box.configure(state="disabled")

    # --- Tab 2: Beat Slicer ---
    add_path_row(tab_beats, "Input Audio File:", slice_input_var, dir_mode=False)
    add_path_row(tab_beats, "Output Folder:", slice_output_var, dir_mode=True)

    beat_opts = ttk.Frame(tab_beats)
    beat_opts.pack(fill="x", pady=6)
    ttk.Label(beat_opts, text="Milestones:", width=20).pack(side="left")
    ttk.Entry(beat_opts, textvariable=slice_marks_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Combobox(
        beat_opts,
        textvariable=slice_mode_var,
        state="readonly",
        values=("timestamps", "durations"),
        width=12,
    ).pack(side="left")

    fade_opts = ttk.Frame(tab_beats)
    fade_opts.pack(fill="x", pady=6)
    ttk.Label(fade_opts, text="Fade In (s):").pack(side="left")
    ttk.Entry(fade_opts, textvariable=slice_fade_in_var, width=6).pack(side="left", padx=(6, 14))
    ttk.Label(fade_opts, text="Fade Out (s):").pack(side="left")
    ttk.Entry(fade_opts, textvariable=slice_fade_out_var, width=6).pack(side="left", padx=(6, 14))
    ttk.Label(fade_opts, text="Format:").pack(side="left")
    ttk.Combobox(
        fade_opts,
        textvariable=slice_ext_var,
        state="readonly",
        values=(".m4a", ".mp3", ".wav"),
        width=8,
    ).pack(side="left", padx=6)

    def run_slicer() -> None:
        in_audio = slice_input_var.get().strip()
        out_dir = slice_output_var.get().strip()
        if not in_audio or not out_dir:
            messagebox.showerror("Missing Paths", "Select an input audio file and output directory.", parent=app)
            return
        try:
            ffmpeg_bin = resolve_ffmpeg_executable()
            files = slice_audio_by_milestones(
                ffmpeg_bin=ffmpeg_bin,
                input_audio=Path(in_audio),
                output_dir=Path(out_dir),
                milestones=slice_marks_var.get(),
                mode=slice_mode_var.get(),
                fade_in=float(slice_fade_in_var.get()),
                fade_out=float(slice_fade_out_var.get()),
                output_ext=slice_ext_var.get(),
            )
            messagebox.showinfo("Audio Sliced", f"Exported {len(files)} segment(s) to {out_dir}.", parent=app)
        except Exception as exc:
            messagebox.showerror("Slice Failed", str(exc), parent=app)

    ttk.Button(tab_beats, text="Slice Audio Segments", style="Primary.TButton", command=run_slicer).pack(anchor="w", pady=10)

    # --- Tab 3: Dedup & License ---
    add_path_row(tab_license, "SQLite History DB:", db_var, dir_mode=False)

    def refresh_dedup_stats() -> None:
        try:
            with MediaHistoryStore(Path(db_var.get().strip())) as store:
                st = store.stats()
            msg = f"Recorded {st['processed_sources']} unique source hash(es) across {st['rendered_outputs']} rendered output(s)."
            if input_var.get().strip() and Path(input_var.get().strip()).is_dir():
                found = collect_visual_sources(Path(input_var.get().strip()))
                with MediaHistoryStore(Path(db_var.get().strip())) as store:
                    fresh = store.filter_unprocessed(found)
                msg += f" Current folder: {len(fresh)}/{len(found)} fresh sources ready."
            dedup_stats_var.set(msg)
        except Exception as exc:
            dedup_stats_var.set(f"Error inspecting DB: {exc}")

    ttk.Label(tab_license, textvariable=dedup_stats_var, style="Muted.TLabel").pack(anchor="w", pady=(4, 8))
    ttk.Button(tab_license, text="Refresh Dedup Stats", command=refresh_dedup_stats).pack(anchor="w", pady=(0, 16))

    lic_box = ttk.Frame(tab_license)
    lic_box.pack(fill="x", pady=6)
    ttk.Label(lic_box, text="Pro License Key:", width=20).pack(side="left")
    ttk.Entry(lic_box, textvariable=license_entry_var).pack(side="left", fill="x", expand=True, padx=(0, 8))

    def activate_key() -> None:
        try:
            status = save_license_key(license_entry_var.get())
            refresh_license_banner()
            messagebox.showinfo("Pro Unlocked", status.reason, parent=app)
        except ValueError as exc:
            messagebox.showerror("Invalid Key", str(exc), parent=app)

    def clear_key() -> None:
        clear_license_key()
        license_entry_var.set("")
        refresh_license_banner()
        messagebox.showinfo("License Cleared", "Switched back to Community Edition.", parent=app)

    lic_actions = ttk.Frame(tab_license)
    lic_actions.pack(fill="x", pady=6)
    ttk.Button(lic_actions, text="Activate Pro Key", style="Primary.TButton", command=activate_key).pack(side="left")
    ttk.Button(lic_actions, text="Clear Key", command=clear_key).pack(side="left", padx=8)
    ttk.Button(lic_actions, text="Get Pro License ($19)", command=lambda: webbrowser.open(PRICING_URL)).pack(side="right")

    def poll_queue() -> None:
        try:
            while True:
                kind, msg = log_queue.get_nowait()
                if kind == "log":
                    append_log(msg)
                else:
                    busy_state["running"] = False
                    run_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
                    append_log(msg)
                    if kind == "error":
                        messagebox.showerror("Render Error", msg, parent=app)
                    else:
                        refresh_dedup_stats()
                        messagebox.showinfo("Batch Complete", msg, parent=app)
        except queue.Empty:
            pass
        app.after(120, poll_queue)

    refresh_license_banner()
    refresh_dedup_stats()
    app.after(120, poll_queue)
    app.mainloop()


def main() -> None:
    launch_gui()


if __name__ == "__main__":
    main()

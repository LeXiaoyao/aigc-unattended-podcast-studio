#!/usr/bin/env python3
"""One-click unattended podcast workflow runner (Solo E1 + Dual-Host E2 + RSS / Xiaoyuzhou Publishing).

Production Architecture for Solo E1:
  1. [03:30] Collect & filter multi-source news into `episodes/YYYYMMDDE1/search_results.json`
  2. [04:30] Codex Agent (`solo-6` / `prompts/solo_daily_script_prompt.md`) writes the final script
             (`podcast_script.txt` + `title.txt` + `script_quality_report.md`) directly from `search_results.json`
             (bypasses legacy Python Qwen/DeepSeek draft chain)
  3. [05:00] Codex Agent (`codex-shadow-mode` / `prompts/solo_editorial_cover_prompt.md`) generates the 1:1
             editorial cover (`cover.jpeg`) directly from the Codex script
  4. [05:15] `solo_pipeline.py synthesize-tts` runs MiniMax Speech-02-HD chunked TTS + `ffmpeg` mixing (-14 LUFS)
             + real-audio Shownotes timeline alignment, then publishes to Standard Podcast RSS 2.0 & Xiaoyuzhou.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def load_env_files() -> None:
    for rel in ("shared/config/.env.listenhub", ".env.listenhub", ".env"):
        env_file = ROOT_DIR / rel
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val


def log(msg: str) -> None:
    ts = dt.datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [unattended] {msg}", flush=True)


def run_cmd(cmd: list[str], env: dict[str, str] | None = None, check: bool = True) -> int:
    log(f"Running: {' '.join(cmd)}")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, cwd=str(ROOT_DIR), env=merged_env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit={proc.returncode}): {' '.join(cmd)}")
    return proc.returncode


def find_latest_episode_dir(flow_type: str | None = None) -> Path | None:
    from rss_publisher import EPISODE_ID_RE, _detect_flow_type, _load_json

    candidates: dict[str, Path] = {}
    for search_root in (ROOT_DIR / "episodes", ROOT_DIR):
        if not search_root.is_dir():
            continue
        for d in search_root.iterdir():
            if d.is_dir() and EPISODE_ID_RE.match(d.name):
                candidates[d.name] = d

    for ep_id in sorted(candidates.keys(), reverse=True):
        ep_dir = candidates[ep_id]
        if not flow_type:
            return ep_dir
        record = _load_json(ep_dir / "record.json")
        if _detect_flow_type(ep_dir, record) == flow_type:
            return ep_dir
    return None


def _run_codex_prompt(prompt_file: Path, extra_instruction: str, dry_run: bool = False) -> bool:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        log("`codex` CLI not found in PATH; skipping direct Codex CLI invocation.")
        return False
    prompt_text = prompt_file.read_text(encoding="utf-8") + "\n\n" + extra_instruction
    cmd = [codex_bin, "exec", "--full-auto", prompt_text]
    if dry_run:
        log(f"[DRY-RUN] Would execute Codex Agent with prompt: {prompt_file.name}")
        return True
    run_cmd(cmd)
    return True


def run_solo_flow(args: argparse.Namespace) -> Path | None:
    log(f"=== Starting Solo Daily Briefing Flow (E1) [script_engine={args.script_engine}] ===")

    if args.script_engine == "codex":
        # Step 1: Collect candidate news into episodes/YYYYMMDDE1/search_results.json
        collect_cmd = [
            sys.executable,
            str(ROOT_DIR / "solo_pipeline.py"),
            "build-episode",
            "--cover-provider",
            "none",
            "--draft-only",
            "--force",
        ]
        if args.dry_run:
            log(f"[DRY-RUN] Step 1 (News Funnel -> search_results.json): {' '.join(collect_cmd)}")
        else:
            run_cmd(collect_cmd)

        ep_dir = find_latest_episode_dir("solo")
        ep_id = ep_dir.name if ep_dir else "YYYYMMDDE1"

        # Step 2: Primary Script Engine — Codex Agent (`solo-6`) writes script directly from search_results.json
        script_prompt = ROOT_DIR / "prompts" / "solo_daily_script_prompt.md"
        if script_prompt.is_file():
            _run_codex_prompt(
                script_prompt,
                f"当前目标单集目录：episodes/{ep_id}。请直接基于 episodes/{ep_id}/search_results.json 写出终稿 podcast_script.txt、title.txt 与 script_quality_report.md，完成后执行 `python3 solo_pipeline.py notify-final-script --episode {ep_id}`。",
                dry_run=args.dry_run,
            )

        # Step 3: Editorial Cover Engine — Codex Agent (`codex-shadow-mode`) generates 1:1 cover.jpeg
        if args.cover_provider == "codex":
            cover_prompt = ROOT_DIR / "prompts" / "solo_editorial_cover_prompt.md"
            if cover_prompt.is_file():
                _run_codex_prompt(
                    cover_prompt,
                    f"当前目标单集目录：episodes/{ep_id}。请基于 episodes/{ep_id}/podcast_script.txt 生成正式封面 cover.jpeg 并更新 cover_generation.json，完成后执行 `python3 solo_pipeline.py notify-cover-ready --episode {ep_id}`。",
                    dry_run=args.dry_run,
                )

        if args.draft_only:
            return ep_dir

        # Step 4: Synthesize MiniMax Speech-02-HD TTS + ffmpeg mixing (-14 LUFS) + real-audio Shownotes
        synth_cmd = [
            sys.executable,
            str(ROOT_DIR / "solo_pipeline.py"),
            "synthesize-tts",
            "--episode",
            ep_id,
        ]
        if args.dry_run:
            log(f"[DRY-RUN] Step 4 (MiniMax TTS + Broadcast Mix + Shownotes): {' '.join(synth_cmd)}")
            return ep_dir
        run_cmd(synth_cmd)
    else:
        # Fallback mode without Codex: run built-in Python LLM chain end-to-end
        cmd = [
            sys.executable,
            str(ROOT_DIR / "solo_pipeline.py"),
            "build-episode",
            "--cover-provider",
            args.cover_provider,
            "--force",
        ]
        if args.draft_only:
            cmd.append("--draft-only")
        if args.dry_run:
            log(f"[DRY-RUN] Would execute Python fallback pipeline: {' '.join(cmd)}")
            return find_latest_episode_dir("solo")
        run_cmd(cmd)

    ep_dir = find_latest_episode_dir("solo")
    if ep_dir:
        log(f"Solo episode directory ready: {ep_dir}")
        qa_script = ROOT_DIR / "scripts" / "check_solo_v2_script.py"
        script_file = ep_dir / "podcast_script.txt"
        if qa_script.is_file() and script_file.is_file():
            run_cmd([sys.executable, str(qa_script), str(script_file)], check=False)
    return ep_dir


def run_dual_host_flow(args: argparse.Namespace) -> Path | None:
    log("=== Starting Dual-Host Deep Dive Flow (E2) ===")
    cmd = [
        sys.executable,
        str(ROOT_DIR / "ai_news_pipeline.py"),
        "build-episode",
        "--audio-mode",
        args.audio_mode,
        "--cover-provider",
        args.cover_provider,
    ]
    if args.dry_run:
        log(f"[DRY-RUN] Would execute: {' '.join(cmd)}")
        return find_latest_episode_dir("dual_host")

    run_cmd(cmd)
    if args.audio_mode == "listenhub" and args.poll_listenhub:
        dl_script = ROOT_DIR / "listenhub_download_weekly.sh"
        if dl_script.is_file():
            run_cmd(["/bin/zsh", str(dl_script)], check=False)
    return find_latest_episode_dir("dual_host")


def publish_episode(ep_dir: Path | None, args: argparse.Namespace) -> None:
    if args.publish_rss:
        from rss_publisher import write_rss_file

        rss_out = Path(args.rss_output).expanduser()
        if args.dry_run:
            log(f"[DRY-RUN] Would generate RSS 2.0 XML at {rss_out}")
        else:
            write_rss_file(output_path=rss_out, base_url=args.base_url)
            log(f"Updated Podcast RSS 2.0 feed: {rss_out}")

    if args.publish_xiaoyuzhou:
        if not ep_dir:
            log("No episode directory found to publish to Xiaoyuzhou; skipping.")
            return
        xyz_script = ROOT_DIR / "xiaoyuzhou_prepare_episode.sh"
        env = {"DRY_RUN": "true"} if args.dry_run else {}
        run_cmd(["/bin/zsh", str(xyz_script), str(ep_dir)], env=env)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AIGC Unattended Podcast Workflow Runner (Solo + Dual-Host + RSS / Xiaoyuzhou)"
    )
    parser.add_argument(
        "--flow",
        choices=["solo", "dual_host", "both"],
        default="solo",
        help="Podcast flow to execute (default: solo)",
    )
    parser.add_argument(
        "--script-engine",
        choices=["codex", "python"],
        default="codex",
        help="Solo E1 scriptwriting engine: 'codex' (primary production engine, writes directly from search_results.json) or 'python' (fallback LLM chain)",
    )
    parser.add_argument(
        "--cover-provider",
        choices=["codex", "gemini", "none"],
        default="codex",
        help="Cover generation provider (default: codex)",
    )
    parser.add_argument(
        "--audio-mode",
        choices=["listenhub", "volcengine", "none"],
        default="listenhub",
        help="Dual-host audio synthesis provider (default: listenhub)",
    )
    parser.add_argument(
        "--poll-listenhub",
        action="store_true",
        help="Poll and download ListenHub audio after submitting dual-host episode",
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Generate news research & script draft only without running TTS",
    )
    parser.add_argument(
        "--publish-rss",
        action="store_true",
        default=True,
        help="Generate/update standard Podcast RSS 2.0 feed (rss.xml) after run (default: True)",
    )
    parser.add_argument(
        "--no-publish-rss",
        action="store_false",
        dest="publish_rss",
        help="Disable automatic RSS feed generation",
    )
    parser.add_argument(
        "--rss-output",
        default=str(ROOT_DIR / "rss.xml"),
        help="Output path for generated RSS 2.0 XML file",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Public base URL for RSS enclosure links (overrides PODCAST_BASE_URL)",
    )
    parser.add_argument(
        "--publish-xiaoyuzhou",
        action="store_true",
        help="One-click upload & schedule episode on Xiaoyuzhou Podcaster Studio",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing external mutations",
    )
    args = parser.parse_args()

    load_env_files()

    latest_ep: Path | None = None
    if args.flow in ("solo", "both"):
        latest_ep = run_solo_flow(args)
        publish_episode(latest_ep, args)

    if args.flow in ("dual_host", "both"):
        latest_ep = run_dual_host_flow(args)
        publish_episode(latest_ep, args)

    log("Unattended workflow run completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dynamically write launchd plist files and reload agents."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
LAUNCHD_LABEL_PREFIX = os.environ.get("LAUNCHD_LABEL_PREFIX", "com.podcast").strip()


def _build_calendar_intervals(days_of_week: list[int], hour: int, minute: int) -> str:
    """Build StartCalendarInterval XML. If all 7 days, use simple daily dict."""
    if len(days_of_week) == 7:
        return f"""  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>"""

    items = []
    for day in sorted(days_of_week):
        # launchd Weekday: 0=Sun, 1=Mon ... 6=Sat
        # Python weekday: 0=Mon ... 6=Sun — convert
        launchd_day = (day + 1) % 7
        items.append(
            f"    <dict>"
            f"<key>Weekday</key><integer>{launchd_day}</integer>"
            f"<key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>{minute}</integer>"
            f"</dict>"
        )
    intervals = "\n".join(items)
    return f"""  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>"""


def write_plist(
    label: str,
    script_path: str,
    hour: int,
    minute: int,
    days_of_week: list[int],
    out_log: str,
    err_log: str,
) -> Path:
    """Write a launchd plist file and return its path."""
    calendar = _build_calendar_intervals(days_of_week, hour, minute)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>{script_path}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>{ROOT_DIR}</string>

{calendar}

  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>{out_log}</string>

  <key>StandardErrorPath</key>
  <string>{err_log}</string>
</dict>
</plist>
"""
    plist_path = ROOT_DIR / f"{label}.plist"
    plist_path.write_text(content, encoding="utf-8")
    return plist_path


def reload_agent(plist_path: Path) -> None:
    """Unload and reload a launchd user agent."""
    label = plist_path.stem
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    print(f"[launchd] Reloaded: {label}")


def update_dual_host_schedule(enabled: bool, days_of_week: list[int], hour: int, minute: int) -> None:
    plist = write_plist(
        label=f"{LAUNCHD_LABEL_PREFIX}.ai-podcast-generator",
        script_path=str(ROOT_DIR / "run_ai_podcast_weekly.sh"),
        hour=hour,
        minute=minute,
        days_of_week=days_of_week if enabled else [],
        out_log=str(ROOT_DIR / "logs" / "ai-podcast-generator.out.log"),
        err_log=str(ROOT_DIR / "logs" / "ai-podcast-generator.err.log"),
    )
    if enabled:
        reload_agent(plist)


def update_solo_schedule(enabled: bool, days_of_week: list[int], hour: int, minute: int, daily_runs: list[dict] | None = None) -> None:
    """Update solo schedule. Supports multiple daily runs via daily_runs list.

    daily_runs: [{"hour": 4, "minute": 0}, {"hour": 12, "minute": 0}]
    Falls back to single (hour, minute) if daily_runs is empty.
    """
    runs = daily_runs or [{"hour": hour, "minute": minute}]
    # Build calendar intervals for all runs across all days
    all_intervals = []
    for run in runs:
        for day in (days_of_week if enabled else []):
            launchd_day = (day + 1) % 7
            all_intervals.append({"weekday": launchd_day, "hour": run["hour"], "minute": run["minute"]})

    plist = _write_solo_plist_multi(all_intervals)
    if enabled:
        reload_agent(plist)


def _write_solo_plist_multi(intervals: list[dict]) -> Path:
    """Write solo plist with multiple StartCalendarInterval entries."""
    label = f"{LAUNCHD_LABEL_PREFIX}.solo-podcast-generator"
    script_path = str(ROOT_DIR / "run_solo_podcast_daily.sh")
    out_log = str(ROOT_DIR / "logs" / "solo-podcast-generator.out.log")
    err_log = str(ROOT_DIR / "logs" / "solo-podcast-generator.err.log")

    if not intervals:
        # Disabled — write a simple plist with no intervals
        return write_plist(label, script_path, 4, 0, [], out_log, err_log)

    items = []
    for iv in intervals:
        parts = [f"<key>Hour</key><integer>{iv['hour']}</integer>",
                 f"<key>Minute</key><integer>{iv['minute']}</integer>"]
        if "weekday" in iv:
            parts.insert(0, f"<key>Weekday</key><integer>{iv['weekday']}</integer>")
        items.append("    <dict>" + "".join(parts) + "</dict>")

    calendar_xml = "  <key>StartCalendarInterval</key>\n  <array>\n" + "\n".join(items) + "\n  </array>"

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>{script_path}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>{ROOT_DIR}</string>

{calendar_xml}

  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>{out_log}</string>

  <key>StandardErrorPath</key>
  <string>{err_log}</string>
</dict>
</plist>
"""
    plist_path = ROOT_DIR / f"{label}.plist"
    plist_path.write_text(content, encoding="utf-8")
    return plist_path

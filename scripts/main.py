#!/usr/bin/env python3
"""
YouTube Video Downloader + Subtitle Burner — debug-enabled edition.
Each processing step saves intermediate files for inspection.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent.resolve()
CONFIG_PATH = SKILL_DIR / "config.json"


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# ===================== 敏感词 & 控制字符 =====================

SENSITIVE_REPLACEMENTS = [
    ("习近平", "领导人"),
    ("中国共产党", "东大"),
    ("中共", "东大"),
    ("中国", "东大"),
]
XI_PATTERN = re.compile(r'\bX[iI]\b')

# Each tuple: (debug_name, regex_pattern)
# NOTE: the old single-range `\x0E-‏` was a _massive_ bug — it matched
# everything from chr(14) to chr(8207), wiping out ASCII letters/numbers.
# Now each class is explicitly listed.
CONTROL_CHARS = [
    ("C0_ctrl",     r'[\x00-\x08\x0B\x0C\x0E-\x1F]'),   # C0 control chars
    ("DEL",         r'\x7F'),                                # U+007F
    ("zero_width",  r'[​-‏]'),                     # zero-width
    ("BOM",         r'﻿'),                              # U+FEFF
    ("soft_hyphen", r'­'),                              # U+00AD
    ("fmt_ctrl",    r'[⁠-⁯]'),                     # format controls
]

CONTROL_CHARS_PATTERN = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in CONTROL_CHARS)
)


def _report_control_chars(text: str) -> list[str]:
    """Find every invisible/control character for the debug report."""
    found = []
    for m in CONTROL_CHARS_PATTERN.finditer(text):
        char = m.group(0)
        name = m.lastgroup or "unknown"
        ctx_start = max(0, m.start() - 5)
        ctx_end = min(len(text), m.end() + 5)
        ctx = text[ctx_start:ctx_end]
        found.append(
            f"  U+{ord(char):04X} ({name:10s})  pos={m.start():3d}  ctx=...{ctx}..."
        )
    return found

# ===================== 步骤化 SRT 处理 =====================

def _parse_time_ms(time_str: str) -> int:
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def _format_time_ms(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_entries(text: str) -> list[dict]:
    """Robust SRT parser that handles YouTube auto-subtitle quirks."""
    entries = []
    lines = text.strip().splitlines()
    i = 0
    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        stripped = lines[i].strip().lstrip('﻿')
        if not stripped.isdigit():
            i += 1
            continue
        if i + 1 >= len(lines):
            break
        if not re.match(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->', lines[i + 1]):
            i += 1
            continue
        i += 1
        timecode = lines[i]
        i += 1
        if i >= len(lines):
            break
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        if text_lines:
            entries.append({"timecode": timecode, "text_lines": text_lines})
    return entries


def _write_srt_entries(entries: list[dict], filepath: Path) -> None:
    """Write structured entries to SRT file."""
    result = []
    for i, e in enumerate(entries, 1):
        tc = e["timecode"]
        text = e.get("text", "")
        if isinstance(text, list):
            text = "\n".join(text)
        result.append(f"{i}\n{tc}\n{text}")
    filepath.write_text("\n\n".join(result) + "\n\n", encoding="utf-8")


def step_01_parse(filepath: Path, debug_dir: Path) -> dict:
    """Step 1: Parse raw SRT into JSON for inspection."""
    text = filepath.read_text(encoding="utf-8")
    entries = _parse_srt_entries(text)

    serializable = []
    for e in entries:
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', e["timecode"])
        if m:
            serializable.append({
                "start_ms": _parse_time_ms(m.group(1)),
                "end_ms": _parse_time_ms(m.group(2)),
                "text_lines": e["text_lines"],
            })

    debug_json = debug_dir / f"{filepath.stem}_01_parsed.json"
    debug_json.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"    Step 1 - Parsed: {len(entries)} entries → {debug_json.name}")
    return {"entries": entries}


def step_02_clean(entries: list[dict], filepath: Path, debug_dir: Path) -> list[dict]:
    """Step 2: Strip control/invisible chars and HTML/XML tags."""
    cleaned = []
    all_ctrl_reports = []

    for e in entries:
        text = " ".join(e["text_lines"]).strip()

        ctrl_reports = _report_control_chars(text)
        if ctrl_reports:
            all_ctrl_reports.extend(ctrl_reports)
            all_ctrl_reports.append(f"    text: {text[:120]}")
            all_ctrl_reports.append("")

        text = CONTROL_CHARS_PATTERN.sub('', text)
        text = re.sub(r'<[^>]+>', '', text).strip()

        if not text:
            continue

        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', e["timecode"])
        if not m:
            continue

        cleaned.append({
            "start_ms": _parse_time_ms(m.group(1)),
            "end_ms": _parse_time_ms(m.group(2)),
            "text": text,
        })

    debug_srt = debug_dir / f"{filepath.stem}_02_cleaned.srt"
    srt_entries = [
        {"timecode": f"{_format_time_ms(e['start_ms'])} --> {_format_time_ms(e['end_ms'])}", "text": e["text"]}
        for e in cleaned
    ]
    _write_srt_entries(srt_entries, debug_srt)

    if all_ctrl_reports:
        report_file = debug_dir / f"{filepath.stem}_02_ctrl_report.txt"
        report_file.write_text("\n".join(all_ctrl_reports), encoding="utf-8")
        print(f"    Step 2 - {len(cleaned)} entries, ctrl report → {report_file.name}")
    else:
        print(f"    Step 2 - Cleaned: {len(cleaned)} entries → {debug_srt.name}")
    return cleaned


def step_03_filter(cleaned: list[dict], filepath: Path, debug_dir: Path) -> list[dict]:
    """Step 3: Apply sensitive word replacements."""
    filtered = []
    replacements_log = []

    for e in cleaned:
        text = e["text"]
        original = text

        for old, new in SENSITIVE_REPLACEMENTS:
            text = text.replace(old, new)
        text = XI_PATTERN.sub("领导人", text)

        if text != original:
            replacements_log.append(f"  CHANGED:\n    FROM: {original}\n    TO:   {text}")

        if text:
            filtered.append({**e, "text": text})

    debug_srt = debug_dir / f"{filepath.stem}_03_filtered.srt"
    srt_entries = [
        {"timecode": f"{_format_time_ms(e['start_ms'])} --> {_format_time_ms(e['end_ms'])}", "text": e["text"]}
        for e in filtered
    ]
    _write_srt_entries(srt_entries, debug_srt)

    if replacements_log:
        log_file = debug_dir / f"{filepath.stem}_03_filter_log.txt"
        log_file.write_text("\n\n".join(replacements_log), encoding="utf-8")
        print(f"    Step 3 - {len(filtered)} entries, filter log → {log_file.name}")
    else:
        print(f"    Step 3 - Filtered: {len(filtered)} entries → {debug_srt.name}")
    return filtered


def step_04_merge(filtered: list[dict], filepath: Path, debug_dir: Path) -> list[dict]:
    """Step 4: Merge temporally adjacent subtitle entries."""
    if not filtered:
        return []

    filtered.sort(key=lambda x: x["start_ms"])

    for i in range(len(filtered) - 1):
        if filtered[i]["end_ms"] > filtered[i + 1]["start_ms"]:
            filtered[i]["end_ms"] = filtered[i + 1]["start_ms"] - 1

    merged = []
    curr = {
        "start_ms": filtered[0]["start_ms"],
        "end_ms": filtered[0]["end_ms"],
        "text": filtered[0]["text"],
    }

    for nxt in filtered[1:]:
        gap = nxt["start_ms"] - curr["end_ms"]

        zh_curr = len(re.findall(r'[一-鿿]', curr["text"])) > 0
        zh_nxt = len(re.findall(r'[一-鿿]', nxt["text"])) > 0

        if zh_curr or zh_nxt:
            combined = (curr["text"] + nxt["text"]).strip()
            combined = re.sub(r'([一-鿿])\s+([一-鿿])', r'\1\2', combined)
        else:
            combined = (curr["text"] + " " + nxt["text"]).strip()

        zh_combined = len(re.findall(r'[一-鿿]', combined)) > 0
        limit = 100 if zh_combined else 170

        if gap < 2000 and len(combined) <= limit:
            curr["text"] = combined
            curr["end_ms"] = max(curr["end_ms"], nxt["end_ms"])
        else:
            merged.append(curr)
            curr = {
                "start_ms": nxt["start_ms"],
                "end_ms": nxt["end_ms"],
                "text": nxt["text"],
            }

    merged.append(curr)

    debug_srt = debug_dir / f"{filepath.stem}_04_merged.srt"
    srt_entries = [
        {"timecode": f"{_format_time_ms(e['start_ms'])} --> {_format_time_ms(e['end_ms'])}", "text": e["text"]}
        for e in merged
    ]
    _write_srt_entries(srt_entries, debug_srt)

    print(f"    Step 4 - Merged: {len(merged)} entries → {debug_srt.name}")
    return merged


def step_05_wrap(merged: list[dict], filepath: Path, debug_dir: Path) -> None:
    """Step 5: Wrap lines and write final SRT (overwrites original)."""
    final = []
    for i, e in enumerate(merged):
        is_zh = len(re.findall(r'[一-鿿]', e["text"])) > 0
        max_per = 48 if is_zh else 80
        wrapped = _wrap_line(e["text"], max_per_line=max_per)
        final.append({
            "timecode": f"{_format_time_ms(e['start_ms'])} --> {_format_time_ms(e['end_ms'])}",
            "text": wrapped,
        })

    debug_srt = debug_dir / f"{filepath.stem}_05_wrapped.srt"
    _write_srt_entries(final, debug_srt)
    _write_srt_entries(final, filepath)

    print(f"    Step 5 - Wrapped: {len(final)} entries → {filepath.name} (debug: {debug_srt.name})")


def _wrap_line(text: str, max_per_line: int = 20) -> str:
    """Split text into at most 2 lines. Break near middle at punctuation if possible."""
    flat = re.sub(r'([一-鿿])([a-zA-Z\d])', r'\1 \2', text.replace("\n", ""))
    flat = re.sub(r'([a-zA-Z\d])([一-鿿])', r'\1 \2', flat)
    flat = flat.strip()

    if len(flat) <= max_per_line:
        return flat

    mid = len(flat) // 2
    punct = set("，。；：！？、,.:;!? ")
    best = None
    for delta in range(0, 6):
        for pos in [mid + delta, mid - delta]:
            if 0 < pos < len(flat) and flat[pos] in punct:
                best = pos
                break
        if best is not None:
            break

    if best is None:
        best = mid
        return f"{flat[:best].strip()}\n{flat[best:].lstrip()}"
    else:
        return f"{flat[:best+1].strip()}\n{flat[best+1:].lstrip()}"


def process_srt(filepath: Path, debug_dir: Path = None) -> None:
    """Process SRT in 5 steps, saving intermediate files for debugging.

    Steps:
      1. Parse raw SRT → {stem}_01_parsed.json
      2. Clean (ctrl chars, HTML) → {stem}_02_cleaned.srt + _ctrl_report.txt
      3. Sensitive filter → {stem}_03_filtered.srt + _filter_log.txt
      4. Merge adjacent → {stem}_04_merged.srt
      5. Wrap & format → {stem}_05_wrapped.srt, overwrite original
    """
    if debug_dir is None:
        debug_dir = filepath.parent / "debug"
    debug_dir.mkdir(exist_ok=True)

    print(f"    Processing: {filepath.name}")
    print(f"    Debug dir:  {debug_dir}")

    result = step_01_parse(filepath, debug_dir)
    cleaned = step_02_clean(result["entries"], filepath, debug_dir)
    filtered = step_03_filter(cleaned, filepath, debug_dir)
    merged = step_04_merge(filtered, filepath, debug_dir)
    step_05_wrap(merged, filepath, debug_dir)
    print(f"    Done: {filepath.name}")


# ===================== ASS 生成（纯 Python，不依赖 ffmpeg 转换）=====================

def _srt_time_to_ass(srt_time: str) -> str:
    """Convert SRT time 'HH:MM:SS,mmm' to ASS time 'H:MM:SS.cc'."""
    m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', srt_time)
    if not m:
        return "0:00:00.00"
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    # round ms to centiseconds
    total_cs = (h * 3600 + mi * 60 + s) * 100 + ms // 10
    if ms % 10 >= 5:
        total_cs += 1
    h = total_cs // 360000
    total_cs %= 360000
    mi = total_cs // 6000
    total_cs %= 6000
    s = total_cs // 100
    cs = total_cs % 100
    return f"{h}:{mi:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Escape ASS special characters in Dialogue text."""
    text = text.replace("\\", "\\\\")
    text = text.replace("\n", "\\N")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    return text


def generate_ass(srt_path: Path, ass_path: Path, cfg: dict, marginv: int, fontsize: int = None):
    """Generate ASS file directly from SRT — no ffmpeg conversion step."""
    style = cfg["style"]
    srt_text = srt_path.read_text(encoding="utf-8")
    entries = _parse_srt_entries(srt_text)

    lines = [
        "[Script Info]",
        "Title: Generated Subtitle",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,PingFang SC,{fontsize or style['fontsize']},{style['primary_color']},{style['primary_color']},{style['outline_color']},{style['outline_color']},0,0,0,0,100,100,0,0,1,{style['outline']},0,{style['alignment']},10,10,{marginv},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for e in entries:
        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', e["timecode"])
        if not m:
            continue
        start = _srt_time_to_ass(m.group(1))
        end = _srt_time_to_ass(m.group(2))
        text = "\n".join(e["text_lines"]).strip()
        text = _escape_ass_text(text)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===================== 下载 =====================

def safe_filename(title: str) -> str:
    title = title.replace(":", "：").replace("/", "_").replace("\\", "_")
    title = re.sub(r'[<>"">|?*]', "", title)
    return title.strip()


def yt_dlp_base_cmd(cfg: dict) -> list:
    cmd = ["yt-dlp", "--quiet", "--no-warnings"]
    cookies = cfg["download"].get("use_cookies", "")
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    return cmd


def list_videos(channel_url: str, days: int, min_duration: int, max_results: int, cfg: dict) -> list[dict]:
    cutoff = datetime.now() - timedelta(days=days)
    cmd = yt_dlp_base_cmd(cfg) + [
        "--dump-single-json",
        "--playlist-end", str(max_results),
        channel_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Failed to list videos: {result.stderr}", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("Failed to parse video list", file=sys.stderr)
        return []

    videos = []
    for entry in data.get("entries", []):
        vid_id = entry.get("id")
        title = entry.get("title", "Unknown")
        duration = entry.get("duration") or 0
        upload_date_str = entry.get("upload_date")
        if not vid_id or not upload_date_str:
            continue
        try:
            upload_date = datetime.strptime(upload_date_str, "%Y%m%d")
        except ValueError:
            continue
        if upload_date < cutoff:
            continue
        if duration < min_duration:
            continue
        videos.append({
            "id": vid_id,
            "date": upload_date_str,
            "title": safe_filename(title),
            "duration": int(duration),
        })
    return videos


def download_video(video_id: str, temp_dir: Path, cfg: dict) -> Path:
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = temp_dir / f"{video_id}{ext}"
        if candidate.exists():
            print(f"    Video already exists: {candidate.name}")
            return candidate
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = yt_dlp_base_cmd(cfg) + [
        "--sleep-requests", str(cfg["download"]["sleep_requests"]),
        "--extractor-retries", "3",
        "--limit-rate", cfg["download"]["rate_limit"],
        "-f", f"bestvideo[height<={cfg['download']['max_height']}][vcodec^=avc]+bestaudio[acodec^=mp4a]/best",
        "--merge-output-format", "mp4",
        "-o", str(temp_dir / f"{video_id}.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = temp_dir / f"{video_id}{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Video not found: {video_id}")


def download_subtitles(video_id: str, temp_dir: Path, cfg: dict) -> list[Path]:
    existing = list(temp_dir.glob(f"{video_id}.*.srt")) + list(temp_dir.glob(f"{video_id}.*.vtt"))
    if existing:
        print(f"    Subtitles already exist.")
        return sorted(existing)
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = yt_dlp_base_cmd(cfg) + [
        "--sleep-requests", str(cfg["download"]["sleep_requests"]),
        "--extractor-retries", "3",
        "--skip-download", "--write-auto-sub",
        "--sub-langs", cfg["download"]["sub_langs"],
        "--convert-subs", "srt",
        "--sub-format", "srt",
        "-o", str(temp_dir / f"{video_id}.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    files = list(temp_dir.glob(f"{video_id}.*.srt")) + list(temp_dir.glob(f"{video_id}.*.vtt"))
    return sorted(files)


def detect_language(srt_path: Path) -> str:
    text = srt_path.read_text(encoding="utf-8")[:800]
    lines = [l for l in text.splitlines()
             if l.strip() and not l.strip().isdigit() and "-->" not in l]
    content = "".join(lines)
    chinese = len(re.findall(r'[一-鿿]', content))
    return "zh" if chinese / max(len(content), 1) > 0.3 else "en"


# ===================== 烧录 =====================

def burn_subtitles(mp4: Path, sub_files: list[Path], output: Path, cfg: dict, preview_sec: int = None):
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = mp4.parent / ".burn_temp"
    work_dir.mkdir(exist_ok=True)

    ass_files = []
    for sub in sub_files:
        lang = detect_language(sub)
        marginv = cfg["position"]["zh_marginv"] if lang == "zh" else cfg["position"]["en_marginv"]
        ass = work_dir / f"{lang}.ass"
        generate_ass(sub, ass, cfg, marginv)
        ass_files.append(ass)

    filters = ",".join([f"ass='{ass}'" for ass in ass_files])

    cmd = ["ffmpeg", "-y"]
    if preview_sec:
        cmd += ["-ss", "0", "-t", str(preview_sec)]
    cmd += [
        "-i", str(mp4),
        "-vf", filters,
        "-c:v", "libx264",
        "-crf", str(cfg["output"]["crf"]),
        "-preset", cfg["output"]["preset"],
        "-c:a", "copy",
        str(output),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    shutil.rmtree(work_dir)


# === MARKER: main ===

# ===================== 主流程 =====================

def is_single_video_url(url: str) -> bool:
    return "watch?v=" in url or "youtu.be/" in url


def get_single_video(url: str, cfg: dict) -> list[dict]:
    cmd = yt_dlp_base_cmd(cfg) + ["--dump-json", url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Failed to fetch video info: {result.stderr}", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("Failed to parse video info", file=sys.stderr)
        return []
    return [{
        "id": data.get("id"),
        "date": data.get("upload_date", ""),
        "title": safe_filename(data.get("title", "Unknown")),
        "duration": int(data.get("duration") or 0),
    }]


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(description="Download YouTube videos and burn subtitles")
    parser.add_argument("url", nargs="?", default=cfg["download"]["channel_url"],
                        help="YouTube channel URL or single video URL")
    parser.add_argument("--days", type=int, default=cfg["download"]["default_days"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--preview-sec", type=int, default=0, help="Burn only first N seconds as preview")
    args = parser.parse_args()

    output_dir = Path.home() / cfg["download"]["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    record_file = output_dir / "downloaded.json"
    downloaded = {}
    if record_file.exists():
        downloaded = json.loads(record_file.read_text())

    if is_single_video_url(args.url):
        videos = get_single_video(args.url, cfg)
    else:
        videos = list_videos(
            args.url, args.days,
            cfg["download"]["min_duration"],
            cfg["download"]["max_results"],
            cfg,
        )

    new_videos = [v for v in videos if v["id"] not in downloaded]
    if args.limit > 0:
        new_videos = new_videos[:args.limit]

    if not new_videos:
        print(f"No new videos found.")
        return

    print(f"Found {len(new_videos)} new video(s):")
    for v in new_videos:
        dur_m = v["duration"] // 60
        print(f"  [{v['date']}] {v['title']} ({dur_m}min)")

    if args.dry_run:
        print("\n(Dry run, no actual download)")
        return

    for v in new_videos:
        print(f"\nDownloading: {v['title']}")

        final_dir = output_dir / f"[{v['date']}] {v['title']}"
        final_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = final_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        try:
            print("  Fetching subtitles...")
            sub_files = download_subtitles(v["id"], raw_dir, cfg)

            for sub in sub_files:
                process_srt(sub, debug_dir=raw_dir / "debug")

            print("  Downloading video...")
            vid_path = download_video(v["id"], raw_dir, cfg)

            print("  Burning subtitles...")
            if args.preview_sec:
                output_mp4 = final_dir / f"[{v['date']}] {v['title']}_preview.mp4"
            else:
                output_mp4 = final_dir / f"[{v['date']}] {v['title']}_burned.mp4"
            burn_subtitles(vid_path, sub_files, output_mp4, cfg,
                           preview_sec=args.preview_sec if args.preview_sec > 0 else None)

            if not args.preview_sec:
                downloaded[v["id"]] = datetime.now().isoformat()
                record_file.write_text(json.dumps(downloaded, indent=2), encoding="utf-8")
            print("  Done")

        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()

        if v != new_videos[-1]:
            time.sleep(cfg["download"]["sleep_interval"])

    print(f"\nAll done. Output directory: {output_dir}")


if __name__ == "__main__":
    main()

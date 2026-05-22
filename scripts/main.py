#!/usr/bin/env python3
"""
YouTube Video Downloader + Subtitle Burner

Downloads YouTube videos with Chinese and English subtitles,
processes them (sensitive word replacement), and burns subtitles
as hardcoded text into the video.
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


# ===================== 敏感词处理 =====================

SENSITIVE_REPLACEMENTS = [
    ("习近平", "领导人"),
    ("中国共产党", "东大"),
    ("中共", "东大"),
    ("中国", "东大"),
]
XI_PATTERN = re.compile(r'\bX[iI]\b')


def wrap_line(text: str, max_per_line: int = 20) -> str:
    """Split text into at most 2 lines. Break near the middle at punctuation if possible."""
    # Ensure spaces between CJK and Latin characters
    flat = re.sub(r'([一-鿿])([a-zA-Z\d])', r' ', text.replace("\n", ""))
    flat = re.sub(r'([a-zA-Z\d])([一-鿿])', r' ', flat)
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
        # Punctuation goes to the end of first line, not the start of second
        return f"{flat[:best+1].strip()}\n{flat[best+1:].lstrip()}"


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


def _is_chinese(text: str) -> bool:
    return len(re.findall(r'[一-鿿]', text)) > 0


def process_srt(filepath: Path) -> None:
    text = filepath.read_text(encoding="utf-8")

    # Parse SRT entries
    entries_raw = re.split(r'\n{2,}', text.strip())
    parsed = []

    for entry in entries_raw:
        lines = entry.splitlines()
        if len(lines) < 3:
            continue

        timecode = lines[1]
        text_lines = [l for l in lines[2:] if l.strip()]
        merged_text = " ".join(text_lines)

        # Remove YouTube auto-subtitle control chars (\x01, \x02, etc.) and HTML/XML tags
        merged_text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+', ' ', merged_text)
        merged_text = re.sub(r'<[^>]+>', '', merged_text).strip()

        # Sensitive word replacement
        for old, new in SENSITIVE_REPLACEMENTS:
            merged_text = merged_text.replace(old, new)
        merged_text = XI_PATTERN.sub("领导人", merged_text)

        if not merged_text:
            continue

        m = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', timecode)
        if not m:
            continue

        start_ms = _parse_time_ms(m.group(1))
        end_ms = _parse_time_ms(m.group(2))
        parsed.append({"start": start_ms, "end": end_ms, "text": merged_text})

    if not parsed:
        return

    # Sort by start time and resolve overlaps
    parsed.sort(key=lambda x: x["start"])
    for i in range(len(parsed) - 1):
        if parsed[i]["end"] > parsed[i+1]["start"]:
            parsed[i]["end"] = parsed[i+1]["start"] - 1

    # Merge entries that are close in time and short in text
    merged_entries = []
    curr = parsed[0]

    for nxt in parsed[1:]:
        gap = nxt["start"] - curr["end"]

        # Join with or without space depending on dominant language
        if _is_chinese(curr["text"]) or _is_chinese(nxt["text"]):
            combined_text = (curr["text"] + nxt["text"]).strip()
            # Remove spaces between Chinese characters
            combined_text = re.sub(r'([一-鿿])\s+([一-鿿])', r'\1\2', combined_text)
        else:
            combined_text = (curr["text"] + " " + nxt["text"]).strip()

        is_zh = _is_chinese(combined_text)
        limit = 100 if is_zh else 170

        if gap < 2000 and len(combined_text) <= limit:
            curr["text"] = combined_text
            curr["end"] = max(curr["end"], nxt["end"])
        else:
            merged_entries.append(curr)
            curr = nxt

    merged_entries.append(curr)

    # Apply line wrapping and rebuild SRT
    final_entries = []
    for i, e in enumerate(merged_entries):
        is_zh = _is_chinese(e["text"])
        max_per = 48 if is_zh else 80
        wrapped = wrap_line(e["text"], max_per_line=max_per)
        final_entries.append(
            f"{i+1}\n{_format_time_ms(e['start'])} --> {_format_time_ms(e['end'])}\n{wrapped}"
        )

    filepath.write_text("\n\n".join(final_entries) + "\n", encoding="utf-8")
    print(f"    Processed: {filepath.name}")


# ===================== 下载 =====================

def safe_filename(title: str) -> str:
    title = title.replace(":", "：").replace("/", "_").replace("\\", "_")
    title = re.sub(r'[<>">|?*]', "", title)
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
    # Skip download if already exists
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
    # Skip download if already exists
    existing_files = list(temp_dir.glob(f"{video_id}.*.srt")) + list(temp_dir.glob(f"{video_id}.*.vtt"))
    if existing_files:
        print(f"    Subtitles already exist.")
        return sorted(existing_files)

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


# ===================== ASS 生成 =====================

def detect_language(srt_path: Path) -> str:
    text = srt_path.read_text(encoding="utf-8")[:800]
    lines = [l for l in text.splitlines()
             if l.strip() and not l.strip().isdigit() and "-->" not in l]
    content = "".join(lines)
    chinese = len(re.findall(r'[\u4e00-\u9fff]', content))
    return "zh" if chinese / max(len(content), 1) > 0.3 else "en"


def generate_ass(srt_path: Path, ass_path: Path, cfg: dict, marginv: int, fontsize: int = None):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(srt_path), "-f", "ass", str(ass_path)],
        check=True, capture_output=True,
    )

    style = cfg["style"]
    text = ass_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if line.startswith("Style: Default,"):
            parts = line.split(",")
            parts[1]  = "PingFang SC"
            parts[2]  = str(fontsize or style["fontsize"])
            parts[3]  = style["primary_color"]
            parts[4]  = style["primary_color"]
            parts[5]  = style["outline_color"]
            parts[6]  = style["outline_color"]
            parts[15] = "1"
            parts[16] = str(style["outline"])
            parts[17] = "0"
            parts[18] = str(style["alignment"])
            parts[19] = "10"
            parts[20] = "10"
            parts[21] = str(marginv)
            parts[22] = "1"
            lines[i] = ",".join(parts)

    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===================== 烧录 =====================

def burn_subtitles(mp4: Path, sub_files: list[Path], output: Path, cfg: dict, preview_sec: int = None):
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = mp4.parent / ".burn_temp"
    work_dir.mkdir(exist_ok=True)

    ass_files = []
    for sub in sub_files:
        lang = detect_language(sub)
        marginv = 50 if lang == "zh" else cfg["position"]["en_marginv"]
        fontsize = 12
        ass = work_dir / f"{lang}.ass"
        generate_ass(sub, ass, cfg, marginv, fontsize=fontsize)
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


# ===================== 主流程 =====================

def is_single_video_url(url: str) -> bool:
    return "watch?v=" in url or "youtu.be/" in url


def get_single_video(url: str, cfg: dict) -> list[dict]:
    """Fetch metadata for a single video URL."""
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

    # Load record
    record_file = output_dir / "downloaded.json"
    downloaded = {}
    if record_file.exists():
        downloaded = json.loads(record_file.read_text())

    # List videos — single URL or channel scan
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
            # 1. Download subtitles
            print("  Fetching subtitles...")
            sub_files = download_subtitles(v["id"], raw_dir, cfg)

            # 2. Process subtitles
            for sub in sub_files:
                process_srt(sub)

            # 3. Download video
            print("  Downloading video...")
            vid_path = download_video(v["id"], raw_dir, cfg)

            # 4. Burn subtitles
            print("  Burning subtitles...")
            if args.preview_sec:
                output_mp4 = final_dir / f"[{v['date']}] {v['title']}_preview.mp4"
            else:
                output_mp4 = final_dir / f"[{v['date']}] {v['title']}_burned.mp4"
            burn_subtitles(vid_path, sub_files, output_mp4, cfg,
                           preview_sec=args.preview_sec if args.preview_sec > 0 else None)

            # 5. Record completion
            if not args.preview_sec:
                downloaded[v["id"]] = datetime.now().isoformat()
                record_file.write_text(json.dumps(downloaded, indent=2), encoding="utf-8")
            print("  Done")

        except Exception as e:
            print(f"  Error: {e}")
            # Do NOT remove raw_dir on error so it can be reused later

        # Rate limiting between videos
        if v != new_videos[-1]:
            time.sleep(cfg["download"]["sleep_interval"])

    print(f"\nAll done. Output directory: {output_dir}")


if __name__ == "__main__":
    main()

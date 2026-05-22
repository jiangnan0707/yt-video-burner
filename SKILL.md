---
name: yt-video-burner
description: Download YouTube videos (latest from a channel or a specific video URL) with Chinese and English subtitles, process them (including sensitive word replacement), and burn the subtitles as hardcoded text into the video file. Use whenever the user wants to download YouTube videos with burned-in bilingual subtitles, mentions "下载YouTube并烧录字幕", "YouTube字幕烧录", "download and burn subtitles", "YouTube hardcoded subtitles", or wants the latest video from any YouTube channel with permanent Chinese and English text overlays. Also triggers when the user mentions yt-dlp combined with ffmpeg subtitle burning, or wants to batch download and burn videos from a channel.
---

# YouTube Video Burner

Download YouTube videos with Chinese and English subtitles, and burn them as hardcoded subtitles into the video. Supports downloading the latest videos from a channel or processing a specific video URL.

## Workflow

1. **Identify target** — determine if the user wants:
   - Latest videos from a channel (specify channel URL and days back)
   - A specific video (provide video URL)
2. **List candidates** — use `yt-dlp` to fetch metadata and filter by date/duration
3. **Confirm with user** — show matching videos; proceed automatically only if user has already confirmed, `--dry-run` was used, or running in batch mode
4. **Download** — download video + EN/ZH subtitles with rate limiting and retries
5. **Process subtitles** — replace sensitive words in Chinese subtitles
6. **Burn** — convert SRT to ASS with styling, then burn into video using ffmpeg
7. **Clean up** — remove temporary files, keep the final burned MP4

## Commands

```bash
# Default: download latest 30 days from configured channel
python3 ~/.claude/skills/yt-video-burner/scripts/main.py

# Specific channel, last 7 days
python3 ~/.claude/skills/yt-video-burner/scripts/main.py "https://www.youtube.com/@channel/videos" --days 7

# Dry run: list what would be downloaded
python3 ~/.claude/skills/yt-video-burner/scripts/main.py --dry-run

# Limit to N videos
python3 ~/.claude/skills/yt-video-burner/scripts/main.py --limit 2
```

## Configuration

Edit `~/.claude/skills/yt-video-burner/config.json` to change defaults.

### Download
| Key | Default | Description |
|-----|---------|-------------|
| `download.channel_url` | `https://www.youtube.com/@allin/videos` | Default channel to check |
| `download.default_days` | 30 | How many days back to look |
| `download.max_height` | 2160 | Max video resolution (4K) |
| `download.min_duration` | 1800 | Skip videos shorter than this (seconds) |
| `download.max_results` | 50 | How many recent videos to scan |
| `download.rate_limit` | "5M" | Download speed limit |
| `download.sub_langs` | "en,zh-Hans" | Subtitle languages to fetch |
| `download.output_dir` | "Movies/AllInPodcast" | Output directory (relative to home) |
| `download.sleep_requests` | 3 | Seconds between API requests |
| `download.sleep_interval` | 15 | Seconds between video downloads |
| `download.use_cookies` | "chrome" | Browser for cookies; set to "" to disable |

### Subtitle Style
| Key | Default | Description |
|-----|---------|-------------|
| `style.fontsize` | 15 | Subtitle font size |
| `style.outline` | 2 | Outline thickness (scaled with video) |
| `style.primary_color` | "&H00FFFFFF" | White text |
| `style.outline_color` | "&H00000000" | Black outline |
| `style.alignment` | 2 | Bottom-center |

### Position
| Key | Default | Description |
|-----|---------|-------------|
| `position.en_marginv` | 15 | English subtitle bottom margin |
| `position.zh_marginv` | 35 | Chinese subtitle bottom margin |

### Output
| Key | Default | Description |
|-----|---------|-------------|
| `output.crf` | 20 | Video quality (lower = better) |
| `output.preset` | "medium" | Encoding speed preset |

## Requirements

- `yt-dlp` — for downloading videos and subtitles
- `ffmpeg` with `libass` support — for subtitle burning
- Python 3.9+

## Notes

- **Sensitive word replacement** is applied to Chinese subtitles automatically (e.g., "习近平" → "领导人", "中国" → "东大").
- **Language auto-detection**: the script determines which subtitle track is English and which is Chinese by character content ratio.
- **ASS coordinate scaling**: ffmpeg converts SRT to ASS with `PlayResY=288`. On a 1080p video, `MarginV` values scale by ~3.75x. For example, `MarginV=15` ≈ 56px from the bottom edge.
- **Track record**: downloaded video IDs are stored in `<output_dir>/downloaded.json` to avoid re-downloading.
- Temp files are created in `<output_dir>/.dl_<video_id>/` and deleted after successful burn.

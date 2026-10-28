"""Video downloader for Instagram, TikTok, X (Twitter) using yt-dlp.

Features:
- Accept URLs via voice, Telegram, or local interface
- Validate URLs and reject unrelated/dangerous schemes
- Identify supported platforms and available formats
- Download publicly accessible videos where permitted
- Merge audio/video using FFmpeg
- Handle rate limits, network errors, extractor changes gracefully
- Save to configurable download directory with meaningful filenames
- Download history tracking
- Progress reporting
- File validation
- Optional MP4 conversion
- Watermark-free preference when authorized source available
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .config import DATA
from .sqlite_safe import closing

log = logging.getLogger("video_downloader")

DB_PATH = DATA / "downloads.db"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "Jarvis"


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    X = "x"
    YOUTUBE = "youtube"
    UNKNOWN = "unknown"


class DownloadState(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class VideoInfo:
    platform: Platform
    url: str
    title: str = ""
    uploader: str = ""
    duration: int | None = None
    thumbnail_url: str = ""
    formats: list[dict] = field(default_factory=list)
    is_watermarked: bool = True
    has_clean_source: bool = False


@dataclass
class DownloadRecord:
    id: str
    url: str
    platform: Platform
    title: str
    state: DownloadState
    file_path: str | None = None
    file_size: int | None = None
    error: str | None = None
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    format_id: str = ""
    resolution: str = ""
    is_watermarked: bool = True
    retry_count: int = 0
    max_retries: int = 3

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "platform": self.platform.value,
            "title": self.title,
            "state": self.state.value,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "error": self.error,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "format_id": self.format_id,
            "resolution": self.resolution,
            "is_watermarked": self.is_watermarked,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadRecord":
        return cls(
            id=data["id"],
            url=data["url"],
            platform=Platform(data.get("platform", "unknown")),
            title=data.get("title", ""),
            state=DownloadState(data.get("state", "pending")),
            file_path=data.get("file_path"),
            file_size=data.get("file_size"),
            error=data.get("error"),
            progress=data.get("progress", 0.0),
            speed=data.get("speed", ""),
            eta=data.get("eta", ""),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            format_id=data.get("format_id", ""),
            resolution=data.get("resolution", ""),
            is_watermarked=data.get("is_watermarked", True),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
        )


class VideoDownloader:
    """Main video downloader using yt-dlp."""

    # URL patterns for supported platforms
    PLATFORM_PATTERNS = {
        Platform.INSTAGRAM: [
            r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories)/[\w-]+",
            r"https?://(?:www\.)?instagram\.com/reel/[\w-]+",
        ],
        Platform.TIKTOK: [
            r"https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+",
            r"https?://vm\.tiktok\.com/[\w-]+",
            r"https?://(?:www\.)?tiktok\.com/t/[\w-]+",
        ],
        Platform.X: [
            r"https?://(?:www\.)?(?:twitter|x)\.com/[\w]+/status/\d+",
            r"https?://(?:www\.)?(?:twitter|x)\.com/[\w]+/statuses/\d+",
        ],
        Platform.YOUTUBE: [
            r"https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+",
            r"https?://youtu\.be/[\w-]+",
            r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+",
        ],
    }

    # Dangerous URL schemes to reject
    DANGEROUS_SCHEMES = {"javascript", "data", "file", "ftp", "ssh", "telnet"}

    def __init__(
        self,
        download_dir: Path | None = None,
        yt_dlp_path: str | None = None,
        ffmpeg_path: str | None = None,
    ) -> None:
        self.download_dir = download_dir or DEFAULT_DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.yt_dlp_path = yt_dlp_path or shutil.which("yt-dlp") or "yt-dlp"
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self._ensure_db()
        self._active_downloads: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def _ensure_db(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        with closing(DB_PATH) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    file_path TEXT,
                    file_size INTEGER,
                    error TEXT,
                    progress REAL DEFAULT 0.0,
                    speed TEXT DEFAULT '',
                    eta TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    format_id TEXT DEFAULT '',
                    resolution TEXT DEFAULT '',
                    is_watermarked INTEGER DEFAULT 1,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3
                );
                CREATE INDEX IF NOT EXISTS idx_downloads_state ON downloads(state);
                CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at DESC);
                """
            )

    def identify_platform(self, url: str) -> Platform:
        """Identify the platform from a URL."""
        for platform, patterns in self.PLATFORM_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, url, re.IGNORECASE):
                    return platform
        return Platform.UNKNOWN

    def validate_url(self, url: str) -> tuple[bool, str]:
        """Validate a URL for downloading."""
        if not url or not url.strip():
            return False, "Empty URL"

        url = url.strip()

        # Check scheme
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return False, f"Unsupported scheme: {parsed.scheme}. Only http/https allowed."

        if parsed.scheme.lower() in self.DANGEROUS_SCHEMES:
            return False, f"Dangerous scheme blocked: {parsed.scheme}"

        # Check platform
        platform = self.identify_platform(url)
        if platform == Platform.UNKNOWN:
            return False, "Unsupported platform. Supported: Instagram, TikTok, X (Twitter), YouTube"

        return True, ""

    def get_video_info(self, url: str) -> VideoInfo | None:
        """Extract video info without downloading."""
        valid, error = self.validate_url(url)
        if not valid:
            log.warning("Invalid URL for info: %s", error)
            return None

        platform = self.identify_platform(url)

        try:
            cmd = [
                self.yt_dlp_path,
                "--dump-json",
                "--no-download",
                "--no-warnings",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                log.warning("yt-dlp info failed: %s", result.stderr)
                return None

            import json
            data = json.loads(result.stdout)

            formats = data.get("formats", [])
            # Prefer formats with both video and audio, or separate best video+audio
            video_formats = [f for f in formats if f.get("vcodec") != "none"]
            audio_formats = [f for f in formats if f.get("acodec") != "none"]

            # Check for watermark indicators
            is_watermarked = True
            has_clean_source = False
            if platform == Platform.TIKTOK:
                # TikTok often has watermarked and non-watermarked versions
                # Check for "no_watermark" or similar in format notes
                for f in formats:
                    if "no_watermark" in f.get("format_note", "").lower():
                        has_clean_source = True
                        is_watermarked = False
                        break

            return VideoInfo(
                platform=platform,
                url=url,
                title=data.get("title", "Unknown"),
                uploader=data.get("uploader", "Unknown"),
                duration=data.get("duration"),
                thumbnail_url=data.get("thumbnail", ""),
                formats=formats,
                is_watermarked=is_watermarked,
                has_clean_source=has_clean_source,
            )
        except subprocess.TimeoutExpired:
            log.error("Video info extraction timed out")
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to get video info: %s", exc)
            return None

    def _build_ydl_cmd(
        self,
        url: str,
        output_path: Path,
        format_selector: str = "bestvideo+bestaudio/best",
        prefer_no_watermark: bool = True,
    ) -> list[str]:
        """Build yt-dlp command with appropriate options."""
        cmd = [
            self.yt_dlp_path,
            "--no-warnings",
            "--ffmpeg-location", self.ffmpeg_path,
            "-o", str(output_path),
            "--merge-output-format", "mp4",
            "--recode-video", "mp4",
            "--embed-metadata",
            "--embed-thumbnail",
            "--add-metadata",
            "-f", format_selector,
        ]

        # Platform-specific options
        if "instagram" in url:
            cmd.extend(["--extractor-args", "instagram:api=graphql"])
        elif "tiktok" in url:
            if prefer_no_watermark:
                # Try to get non-watermarked version
                cmd.extend(["--extractor-args", "tiktok:no_watermark=1"])

        cmd.append(url)
        return cmd

    def _generate_filename(self, info: VideoInfo, ext: str = "mp4") -> str:
        """Generate a meaningful filename."""
        # Clean title for filesystem
        safe_title = re.sub(r'[<>:"/\\|?*]', "_", info.title)
        safe_title = safe_title[:100]  # Limit length
        safe_uploader = re.sub(r'[<>:"/\\|?*]', "_", info.uploader)
        safe_uploader = safe_uploader[:50]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]

        if safe_title and safe_title != "Unknown":
            return f"{safe_uploader}_{safe_title}_{timestamp}_{unique}.{ext}"
        else:
            return f"{safe_uploader}_{platform.value}_{timestamp}_{unique}.{ext}"

    def download(
        self,
        url: str,
        progress_callback: Callable[[DownloadRecord], None] | None = None,
        prefer_no_watermark: bool = True,
        convert_to_mp4: bool = True,
        max_retries: int = 3,
    ) -> DownloadRecord:
        """Download a video from the given URL."""
        valid, error = self.validate_url(url)
        if not valid:
            record = DownloadRecord(
                id=uuid.uuid4().hex,
                url=url,
                platform=Platform.UNKNOWN,
                title="",
                state=DownloadState.FAILED,
                error=error,
            )
            self._save_record(record)
            return record

        platform = self.identify_platform(url)
        info = self.get_video_info(url)

        record = DownloadRecord(
            id=uuid.uuid4().hex,
            url=url,
            platform=platform,
            title=info.title if info else "Unknown",
            state=DownloadState.PENDING,
            max_retries=max_retries,
            is_watermarked=info.is_watermarked if info else True,
        )
        self._save_record(record)

        if progress_callback:
            progress_callback(record)

        # Generate output filename
        filename = self._generate_filename(info or VideoInfo(platform=platform, url=url, title="Unknown"))
        output_path = self.download_dir / filename
        temp_path = self.download_dir / f".{record.id}.{filename}"

        attempt = 0
        while attempt <= max_retries:
            try:
                record.state = DownloadState.DOWNLOADING
                record.started_at = time.time()
                record.retry_count = attempt
                self._save_record(record)
                if progress_callback:
                    progress_callback(record)

                # Determine format selector
                format_selector = "bestvideo+bestaudio/best"
                if platform == Platform.TIKTOK and prefer_no_watermark and info and info.has_clean_source:
                    format_selector = "bestvideo[format_note*=no_watermark]+bestaudio/best[format_note*=no_watermark]/bestvideo+bestaudio/best"

                cmd = self._build_ydl_cmd(url, temp_path, format_selector, prefer_no_watermark)

                log.info("Starting download: %s", url)
                log.debug("Command: %s", " ".join(cmd))

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )

                with self._lock:
                    self._active_downloads[record.id] = proc

                # Monitor progress
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    self._parse_progress(line, record)
                    if progress_callback:
                        progress_callback(record)

                proc.wait()
                with self._lock:
                    self._active_downloads.pop(record.id, None)

                if proc.returncode != 0:
                    raise RuntimeError(f"yt-dlp exited with code {proc.returncode}")

                # Verify downloaded file
                if not temp_path.exists():
                    # Try to find the actual file (yt-dlp may rename)
                    candidates = list(self.download_dir.glob(f".{record.id}.*"))
                    if candidates:
                        temp_path = candidates[0]
                    else:
                        raise RuntimeError("Downloaded file not found")

                # Rename to final name
                final_path = self.download_dir / filename
                if final_path.exists():
                    final_path.unlink()
                temp_path.rename(final_path)

                record.file_path = str(final_path)
                record.file_size = final_path.stat().st_size
                record.state = DownloadState.COMPLETED
                record.completed_at = time.time()
                record.progress = 1.0
                record.progress_message = "Download complete"
                self._save_record(record)

                if progress_callback:
                    progress_callback(record)

                log.info("Download completed: %s", final_path)
                return record

            except Exception as exc:  # noqa: BLE001
                log.warning("Download attempt %d failed: %s", attempt + 1, exc)
                attempt += 1
                if attempt <= max_retries:
                    record.state = DownloadState.PENDING
                    record.error = f"Attempt {attempt} failed: {exc}"
                    self._save_record(record)
                    if progress_callback:
                        progress_callback(record)
                    time.sleep(min(2 ** attempt, 30))  # Exponential backoff
                else:
                    record.state = DownloadState.FAILED
                    record.completed_at = time.time()
                    record.error = f"Failed after {max_retries} attempts: {exc}"
                    self._save_record(record)
                    if progress_callback:
                        progress_callback(record)
                    return record

        return record

    def _parse_progress(self, line: str, record: DownloadRecord) -> None:
        """Parse yt-dlp progress output."""
        # Example: [download]  45.2% of 12.34MiB at 1.23MiB/s ETA 00:05
        patterns = [
            r"\[download\]\s+([\d.]+)%\s+of\s+([\d.]+[KMGT]?i?B)\s+at\s+([\d.]+[KMGT]?i?B/s)\s+ETA\s+(\S+)",
            r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+[KMGT]?i?B)\s+at\s+([\d.]+[KMGT]?i?B/s)\s+ETA\s+(\S+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                record.progress = float(match.group(1)) / 100.0
                # size = match.group(2)  # Not used currently
                record.speed = match.group(3)
                record.eta = match.group(4)
                return

        # Check for merging
        if "Merging formats" in line or "ffmpeg" in line.lower():
            record.state = DownloadState.MERGING

        # Check for conversion
        if "Converting" in line or "post-processing" in line.lower():
            record.state = DownloadState.CONVERTING

    def _save_record(self, record: DownloadRecord) -> None:
        with closing(DB_PATH) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO downloads
                (id, url, platform, title, state, file_path, file_size, error,
                 progress, speed, eta, created_at, started_at, completed_at,
                 format_id, resolution, is_watermarked, retry_count, max_retries)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.url,
                    record.platform.value,
                    record.title,
                    record.state.value,
                    record.file_path,
                    record.file_size,
                    record.error,
                    record.progress,
                    record.speed,
                    record.eta,
                    record.created_at,
                    record.started_at,
                    record.completed_at,
                    record.format_id,
                    record.resolution,
                    1 if record.is_watermarked else 0,
                    record.retry_count,
                    record.max_retries,
                ),
            )

    def get_record(self, download_id: str) -> DownloadRecord | None:
        with closing(DB_PATH) as db:
            row = db.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row:
            return DownloadRecord.from_dict(dict(row))
        return None

    def list_downloads(
        self,
        state: DownloadState | None = None,
        limit: int = 50,
    ) -> list[DownloadRecord]:
        with closing(DB_PATH) as db:
            query = "SELECT * FROM downloads WHERE 1=1"
            params: list = []
            if state:
                query += " AND state = ?"
                params.append(state.value)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
        return [DownloadRecord.from_dict(dict(row)) for row in rows]

    def cancel(self, download_id: str) -> bool:
        """Cancel an active download."""
        with self._lock:
            proc = self._active_downloads.get(download_id)
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self._active_downloads.pop(download_id, None)

        record = self.get_record(download_id)
        if record and record.state in (DownloadState.PENDING, DownloadState.DOWNLOADING, DownloadState.MERGING):
            record.state = DownloadState.CANCELLED
            record.completed_at = time.time()
            record.error = "Cancelled by user"
            self._save_record(record)
            return True
        return False

    def retry(self, download_id: str) -> DownloadRecord | None:
        """Retry a failed download."""
        record = self.get_record(download_id)
        if not record or record.state != DownloadState.FAILED:
            return None

        if record.retry_count >= record.max_retries:
            return None

        # Create new record for retry
        new_record = DownloadRecord(
            id=uuid.uuid4().hex,
            url=record.url,
            platform=record.platform,
            title=record.title,
            state=DownloadState.PENDING,
            max_retries=record.max_retries,
            retry_count=record.retry_count + 1,
            is_watermarked=record.is_watermarked,
        )
        self._save_record(new_record)
        return self.download(
            record.url,
            prefer_no_watermark=not record.is_watermarked,  # Try for clean version on retry
        )

    def cleanup_old(self, days: int = 30) -> int:
        """Remove old completed/failed downloads and their files."""
        cutoff = time.time() - days * 86400
        with closing(DB_PATH) as db:
            rows = db.execute(
                "SELECT id, file_path FROM downloads WHERE state IN (?, ?, ?) AND completed_at < ?",
                (DownloadState.COMPLETED.value, DownloadState.FAILED.value, DownloadState.CANCELLED.value, cutoff),
            ).fetchall()

            for row in rows:
                if row["file_path"] and Path(row["file_path"]).exists():
                    try:
                        Path(row["file_path"]).unlink()
                    except Exception:
                        pass

            cursor = db.execute(
                "DELETE FROM downloads WHERE state IN (?, ?, ?) AND completed_at < ?",
                (DownloadState.COMPLETED.value, DownloadState.FAILED.value, DownloadState.CANCELLED.value, cutoff),
            )
            return cursor.rowcount


# Global instance
_downloader: VideoDownloader | None = None


def get_downloader() -> VideoDownloader:
    global _downloader
    if _downloader is None:
        _downloader = VideoDownloader()
    return _downloader


def download_video(
    url: str,
    progress_callback: Callable[[DownloadRecord], None] | None = None,
    prefer_no_watermark: bool = True,
    convert_to_mp4: bool = True,
) -> DownloadRecord:
    """Convenience function to download a video."""
    return get_downloader().download(url, progress_callback, prefer_no_watermark, convert_to_mp4)
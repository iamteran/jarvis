"""Instagram Stories publishing module.

Features:
- Uses official Instagram Graph API where available
- Checks account eligibility (Business/Creator account required for API)
- Validates video format, duration, size, aspect ratio
- Safe transcoding/resizing when needed
- Explicit confirmation before publishing
- Manual fallback workflow when API not available
- No mandatory paid services (Metricool, Buffer, etc.)
"""
from __future__ import annotations

import logging
import mimetypes
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .config import DATA
from .sqlite_safe import closing

log = logging.getLogger("instagram_publisher")

DB_PATH = DATA / "instagram.db"

# Instagram Stories requirements (as of 2024)
MAX_DURATION_SECONDS = 60
MAX_FILE_SIZE_MB = 100
ASPECT_RATIO = 9 / 16  # 1080x1920
MIN_WIDTH = 720
MIN_HEIGHT = 1280
SUPPORTED_FORMATS = {".mp4", ".mov"}
SUPPORTED_CODECS = {"h264", "hevc"}


class AccountType(str, Enum):
    PERSONAL = "personal"
    CREATOR = "creator"
    BUSINESS = "business"
    UNKNOWN = "unknown"


class PublishState(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    TRANSCODING = "transcoding"
    READY = "ready"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    CANCELLED = "cancelled"


@dataclass
class InstagramAccount:
    id: str
    username: str
    account_type: AccountType
    access_token: str | None = None
    expires_at: float | None = None
    ig_user_id: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_verified: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "account_type": self.account_type.value,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "ig_user_id": self.ig_user_id,
            "connected_at": self.connected_at,
            "last_verified": self.last_verified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstagramAccount":
        return cls(
            id=data["id"],
            username=data["username"],
            account_type=AccountType(data.get("account_type", "unknown")),
            access_token=data.get("access_token"),
            expires_at=data.get("expires_at"),
            ig_user_id=data.get("ig_user_id"),
            connected_at=data.get("connected_at", time.time()),
            last_verified=data.get("last_verified"),
        )


@dataclass
class StoryDraft:
    id: str
    account_id: str
    video_path: str
    caption: str = ""
    state: PublishState = PublishState.PENDING
    validation_errors: list[str] = field(default_factory=list)
    transcoded_path: str | None = None
    media_id: str | None = None
    publish_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    published_at: float | None = None
    requires_manual: bool = False
    manual_instructions: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "video_path": self.video_path,
            "caption": self.caption,
            "state": self.state.value,
            "validation_errors": self.validation_errors,
            "transcoded_path": self.transcoded_path,
            "media_id": self.media_id,
            "publish_url": self.publish_url,
            "error": self.error,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "requires_manual": self.requires_manual,
            "manual_instructions": self.manual_instructions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoryDraft":
        return cls(
            id=data["id"],
            account_id=data["account_id"],
            video_path=data["video_path"],
            caption=data.get("caption", ""),
            state=PublishState(data.get("state", "pending")),
            validation_errors=data.get("validation_errors", []),
            transcoded_path=data.get("transcoded_path"),
            media_id=data.get("media_id"),
            publish_url=data.get("publish_url"),
            error=data.get("error"),
            created_at=data.get("created_at", time.time()),
            published_at=data.get("published_at"),
            requires_manual=data.get("requires_manual", False),
            manual_instructions=data.get("manual_instructions", ""),
        )


class InstagramPublisher:
    """Handles Instagram Stories publishing with API and manual fallback."""

    GRAPH_API_BASE = "https://graph.facebook.com/v19.0"

    def __init__(
        self,
        ffmpeg_path: str | None = None,
        accounts_db: Path | None = None,
        drafts_db: Path | None = None,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self.accounts_db = accounts_db or DB_PATH
        self.drafts_db = drafts_db or DATA / "story_drafts.db"
        self._ensure_dbs()

    def _ensure_dbs(self) -> None:
        self.accounts_db.parent.mkdir(parents=True, exist_ok=True)
        self.drafts_db.parent.mkdir(parents=True, exist_ok=True)

        with closing(self.accounts_db) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    access_token TEXT,
                    expires_at REAL,
                    ig_user_id TEXT,
                    connected_at REAL NOT NULL,
                    last_verified REAL
                );
                """
            )

        with closing(self.drafts_db) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    video_path TEXT NOT NULL,
                    caption TEXT DEFAULT '',
                    state TEXT NOT NULL,
                    validation_errors TEXT DEFAULT '[]',
                    transcoded_path TEXT,
                    media_id TEXT,
                    publish_url TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    published_at REAL,
                    requires_manual INTEGER DEFAULT 0,
                    manual_instructions TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_drafts_state ON drafts(state);
                CREATE INDEX IF NOT EXISTS idx_drafts_account ON drafts(account_id);
                """
            )

    # --------------------------------------------------------------------- Account Management
    def add_account(
        self,
        username: str,
        access_token: str,
        account_type: AccountType = AccountType.UNKNOWN,
        ig_user_id: str | None = None,
    ) -> InstagramAccount:
        """Add or update an Instagram account."""
        import requests

        # Verify token and get account type if unknown
        if account_type == AccountType.UNKNOWN:
            account_type, ig_user_id = self._verify_token(access_token, username)

        account = InstagramAccount(
            id=uuid.uuid4().hex,
            username=username,
            account_type=account_type,
            access_token=access_token,
            ig_user_id=ig_user_id,
            expires_at=time.time() + 3600,  # Short-lived token, will need refresh
        )

        with closing(self.accounts_db) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO accounts
                (id, username, account_type, access_token, expires_at, ig_user_id, connected_at, last_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.id,
                    account.username,
                    account.account_type.value,
                    account.access_token,
                    account.expires_at,
                    account.ig_user_id,
                    account.connected_at,
                    account.last_verified,
                ),
            )

        log.info("Instagram account added: %s (%s)", username, account_type.value)
        return account

    def _verify_token(self, access_token: str, username: str) -> tuple[AccountType, str | None]:
        """Verify access token and determine account type."""
        import requests

        try:
            # Get Instagram user ID
            url = f"{self.GRAPH_API_BASE}/me"
            params = {
                "fields": "id,username,account_type",
                "access_token": access_token,
            }
            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                log.warning("Token verification failed: %s", response.text)
                return AccountType.UNKNOWN, None

            data = response.json()
            ig_user_id = data.get("id")
            account_type_str = data.get("account_type", "").upper()

            if account_type_str == "BUSINESS":
                account_type = AccountType.BUSINESS
            elif account_type_str in ("CREATOR", "CONTENT_CREATOR"):
                account_type = AccountType.CREATOR
            else:
                account_type = AccountType.PERSONAL

            return account_type, ig_user_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Token verification error: %s", exc)
            return AccountType.UNKNOWN, None

    def get_account(self, account_id: str) -> InstagramAccount | None:
        with closing(self.accounts_db) as db:
            row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row:
            return InstagramAccount.from_dict(dict(row))
        return None

    def list_accounts(self) -> list[InstagramAccount]:
        with closing(self.accounts_db) as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY connected_at DESC").fetchall()
        return [InstagramAccount.from_dict(dict(row)) for row in rows]

    def remove_account(self, account_id: str) -> bool:
        with closing(self.accounts_db) as db:
            cursor = db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            return cursor.rowcount > 0

    # --------------------------------------------------------------------- Video Validation
    def validate_video(self, video_path: str) -> tuple[bool, list[str]]:
        """Validate video meets Instagram Stories requirements."""
        errors = []
        path = Path(video_path)

        if not path.exists():
            errors.append(f"File not found: {video_path}")
            return False, errors

        # Check file size
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            errors.append(f"File too large: {size_mb:.1f}MB (max {MAX_FILE_SIZE_MB}MB)")

        # Check format
        if path.suffix.lower() not in SUPPORTED_FORMATS:
            errors.append(f"Unsupported format: {path.suffix}. Use MP4 or MOV")

        # Probe video with ffprobe
        try:
            cmd = [
                self.ffmpeg_path, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration,codec_name",
                "-of", "csv=p=0", str(path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 or not result.stdout.strip():
                errors.append("Could not read video metadata")
                return False, errors

            parts = result.stdout.strip().split(",")
            if len(parts) >= 4:
                width = int(parts[0])
                height = int(parts[1])
                duration = float(parts[2])
                codec = parts[3].strip()

                # Check resolution
                if width < MIN_WIDTH or height < MIN_HEIGHT:
                    errors.append(f"Resolution too low: {width}x{height} (min {MIN_WIDTH}x{MIN_HEIGHT})")

                # Check aspect ratio (allow some tolerance)
                aspect = width / height
                if abs(aspect - ASPECT_RATIO) > 0.05:
                    errors.append(f"Wrong aspect ratio: {aspect:.3f} (required ~{ASPECT_RATIO:.3f}, i.e., 9:16)")

                # Check duration
                if duration > MAX_DURATION_SECONDS:
                    errors.append(f"Video too long: {duration:.1f}s (max {MAX_DURATION_SECONDS}s)")

                # Check codec
                if codec not in SUPPORTED_CODECS:
                    errors.append(f"Unsupported codec: {codec} (supported: h264, hevc)")

        except subprocess.TimeoutExpired:
            errors.append("Video probe timed out")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Video validation error: {exc}")

        return len(errors) == 0, errors

    def transcode_for_stories(self, input_path: str, output_path: str | None = None) -> tuple[bool, str]:
        """Transcode video to meet Instagram Stories specs."""
        if output_path is None:
            output_path = str(Path(input_path).with_stem(Path(input_path).stem + "_stories"))

        try:
            # Transcode to 1080x1920, h264, max 60s, with proper audio
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
                "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-t", str(MAX_DURATION_SECONDS),
                "-movflags", "+faststart",
                output_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                log.error("Transcode failed: %s", result.stderr)
                return False, f"Transcode failed: {result.stderr[:200]}"

            return True, output_path
        except subprocess.TimeoutExpired:
            return False, "Transcode timed out"
        except Exception as exc:  # noqa: BLE001
            return False, f"Transcode error: {exc}"

    # --------------------------------------------------------------------- Draft Management
    def create_draft(
        self,
        account_id: str,
        video_path: str,
        caption: str = "",
    ) -> StoryDraft:
        """Create a story draft for review before publishing."""
        draft = StoryDraft(
            id=uuid.uuid4().hex,
            account_id=account_id,
            video_path=video_path,
            caption=caption,
            state=PublishState.VALIDATING,
        )
        self._save_draft(draft)
        return draft

    def _save_draft(self, draft: StoryDraft) -> None:
        with closing(self.drafts_db) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO drafts
                (id, account_id, video_path, caption, state, validation_errors,
                 transcoded_path, media_id, publish_url, error, created_at,
                 published_at, requires_manual, manual_instructions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    draft.account_id,
                    draft.video_path,
                    draft.caption,
                    draft.state.value,
                    str(draft.validation_errors),
                    draft.transcoded_path,
                    draft.media_id,
                    draft.publish_url,
                    draft.error,
                    draft.created_at,
                    draft.published_at,
                    1 if draft.requires_manual else 0,
                    draft.manual_instructions,
                ),
            )

    def get_draft(self, draft_id: str) -> StoryDraft | None:
        with closing(self.drafts_db) as db:
            row = db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row:
            data = dict(row)
            # Parse validation_errors
            try:
                data["validation_errors"] = eval(data["validation_errors"]) if data["validation_errors"] else []
            except Exception:
                data["validation_errors"] = []
            return StoryDraft.from_dict(data)
        return None

    def list_drafts(
        self,
        account_id: str | None = None,
        state: PublishState | None = None,
    ) -> list[StoryDraft]:
        with closing(self.drafts_db) as db:
            query = "SELECT * FROM drafts WHERE 1=1"
            params: list = []
            if account_id:
                query += " AND account_id = ?"
                params.append(account_id)
            if state:
                query += " AND state = ?"
                params.append(state.value)
            query += " ORDER BY created_at DESC"
            rows = db.execute(query, params).fetchall()
        drafts = []
        for row in rows:
            data = dict(row)
            try:
                data["validation_errors"] = eval(data["validation_errors"]) if data["validation_errors"] else []
            except Exception:
                data["validation_errors"] = []
            drafts.append(StoryDraft.from_dict(data))
        return drafts

    # --------------------------------------------------------------------- Publishing
    def validate_draft(self, draft_id: str) -> tuple[bool, list[str]]:
        """Validate a draft's video file."""
        draft = self.get_draft(draft_id)
        if not draft:
            return False, ["Draft not found"]

        valid, errors = self.validate_video(draft.video_path)
        draft.validation_errors = errors
        draft.state = PublishState.READY if valid else PublishState.FAILED
        self._save_draft(draft)
        return valid, errors

    def prepare_draft(self, draft_id: str) -> tuple[bool, str]:
        """Transcode video if needed."""
        draft = self.get_draft(draft_id)
        if not draft:
            return False, "Draft not found"

        if not draft.validation_errors:
            draft.state = PublishState.READY
            self._save_draft(draft)
            return True, "Already valid"

        draft.state = PublishState.TRANSCODING
        self._save_draft(draft)

        success, result = self.transcode_for_stories(draft.video_path)
        if success:
            draft.transcoded_path = result
            draft.state = PublishState.READY
            draft.validation_errors = []
        else:
            draft.state = PublishState.FAILED
            draft.error = result

        self._save_draft(draft)
        return success, result

    def publish_draft(self, draft_id: str, confirm: bool = False) -> tuple[bool, str]:
        """Publish a story draft to Instagram."""
        draft = self.get_draft(draft_id)
        if not draft:
            return False, "Draft not found"

        if not confirm:
            return False, "Explicit confirmation required"

        account = self.get_account(draft.account_id)
        if not account:
            return False, "Account not found"

        video_path = draft.transcoded_path or draft.video_path

        # Check if we can use API
        if account.account_type in (AccountType.BUSINESS, AccountType.CREATOR) and account.access_token:
            return self._publish_via_api(draft, account, video_path)
        else:
            return self._prepare_manual_publish(draft, account, video_path)

    def _publish_via_api(self, draft: StoryDraft, account: InstagramAccount, video_path: str) -> tuple[bool, str]:
        """Publish using Instagram Graph API."""
        import requests

        try:
            draft.state = PublishState.UPLOADING
            self._save_draft(draft)

            # Step 1: Create media container
            url = f"{self.GRAPH_API_BASE}/{account.ig_user_id}/media"
            params = {
                "media_type": "STORIES",
                "video_url": video_path,  # Must be publicly accessible URL
                "caption": draft.caption,
                "access_token": account.access_token,
            }

            # NOTE: Graph API requires video_url to be publicly accessible
            # For local files, we'd need to upload to a hosting service first
            # This is a limitation - we'll fall back to manual for local files
            if not video_path.startswith("http"):
                return self._prepare_manual_publish(draft, account, video_path)

            response = requests.post(url, data=params, timeout=60)
            if response.status_code != 200:
                raise RuntimeError(f"Media creation failed: {response.text}")

            data = response.json()
            media_id = data.get("id")
            if not media_id:
                raise RuntimeError("No media ID returned")

            # Step 2: Publish the media
            publish_url = f"{self.GRAPH_API_BASE}/{account.ig_user_id}/media_publish"
            publish_params = {
                "creation_id": media_id,
                "access_token": account.access_token,
            }
            response = requests.post(publish_url, data=publish_params, timeout=60)
            if response.status_code != 200:
                raise RuntimeError(f"Publish failed: {response.text}")

            draft.state = PublishState.PUBLISHED
            draft.published_at = time.time()
            draft.media_id = media_id
            self._save_draft(draft)

            log.info("Story published via API: %s", media_id)
            return True, f"Story published successfully (ID: {media_id})"

        except Exception as exc:  # noqa: BLE001
            log.exception("API publish failed: %s", exc)
            draft.state = PublishState.FAILED
            draft.error = str(exc)
            self._save_draft(draft)
            return False, f"API publish failed: {exc}"

    def _prepare_manual_publish(
        self,
        draft: StoryDraft,
        account: InstagramAccount,
        video_path: str,
    ) -> tuple[bool, str]:
        """Prepare for manual publishing via Instagram app."""
        draft.state = PublishState.MANUAL_REQUIRED
        draft.requires_manual = True

        # Copy video to a known location for manual upload
        manual_dir = Path.home() / "Desktop" / "Jarvis_Instagram_Ready"
        manual_dir.mkdir(parents=True, exist_ok=True)
        manual_path = manual_dir / f"story_{draft.id[:8]}.mp4"

        try:
            import shutil
            shutil.copy2(video_path, manual_path)
        except Exception as exc:
            log.warning("Could not copy to manual directory: %s", exc)

        instructions = (
            f"Manual publishing required for @{account.username}.\n"
            f"1. Open Instagram on your phone\n"
            f"2. Swipe right or tap '+' > Story\n"
            f"3. Select the video from: {manual_path}\n"
            f"4. Add caption: {draft.caption}\n"
            f"5. Tap 'Your Story' to publish"
        )
        draft.manual_instructions = instructions
        draft.publish_url = str(manual_path)
        self._save_draft(draft)

        log.info("Manual publish prepared for %s", account.username)
        return True, instructions

    # --------------------------------------------------------------------- Convenience Methods
    def publish_video(
        self,
        account_id: str,
        video_path: str,
        caption: str = "",
        confirm: bool = False,
    ) -> tuple[bool, str]:
        """Convenience: create, validate, prepare, and publish in one call."""
        draft = self.create_draft(account_id, video_path, caption)

        valid, errors = self.validate_draft(draft.id)
        if not valid:
            return False, f"Validation failed: {'; '.join(errors)}"

        self.prepare_draft(draft.id)

        return self.publish_draft(draft.id, confirm=confirm)


# Need to import shutil at top
import shutil
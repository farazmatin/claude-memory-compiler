"""Collect finished meeting audio from private Google Drive folders.

Drive is the durable raw-audio archive. This module downloads an immutable copy
only long enough for the existing compiler to produce its retained transcript.
The Google client is intentionally behind a tiny protocol so policy tests never
need credentials or a network connection.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pipeline import db, ingest
from pipeline.config import (
    AUDIO_DIR,
    DRIVE_BACKFILL_CUTOFF,
    DRIVE_BACKFILL_FOLDER_ID,
    DRIVE_CREDENTIALS_FILE,
    DRIVE_FUTURE_FOLDER_ID,
    DRIVE_HANDOFF_DIR,
    DRIVE_SCOPES,
    DRIVE_TOKEN_FILE,
    TZ,
)


class CaptureError(RuntimeError):
    """The Drive capture stage cannot safely continue."""


@dataclass(frozen=True)
class CaptureSource:
    """One approved Drive folder and its ingestion policy."""

    folder_id: str
    kind: str


@dataclass(frozen=True)
class DriveFile:
    """Metadata needed to safely stage a Drive file."""

    file_id: str
    version: str
    name: str
    mime_type: str | None
    byte_size: int | None
    md5_checksum: str | None
    created_time: str | None
    modified_time: str | None
    web_view_link: str | None


class DriveClient(Protocol):
    """The small Drive surface required by capture and rehydration."""

    def list_files(self, folder_id: str) -> list[DriveFile]: ...

    def get_file(self, file_id: str) -> DriveFile: ...

    def download(self, file_id: str, destination: Path) -> None: ...


def authorize() -> None:
    """Open the one-time desktop OAuth flow and persist the private refresh token."""
    _credentials(interactive=True)
    print(f"Drive authorization saved outside the repository: {DRIVE_TOKEN_FILE}")


def run(
    *,
    dry_run: bool = False,
    client: DriveClient | None = None,
    db_path: Path | None = None,
    inbox: Path | None = None,
    sources: list[CaptureSource] | None = None,
) -> dict[str, int]:
    """Stage new audio from configured Drive folders for normal ingestion."""
    db.init_db(db_path)
    active_sources = sources if sources is not None else configured_sources(db_path)
    counts = _empty_counts()
    if not active_sources:
        return counts

    drive = client or google_drive_client()
    handoff_dir = (inbox or DRIVE_HANDOFF_DIR).resolve()
    for source in active_sources:
        for remote in drive.list_files(source.folder_id):
            counts["scanned"] += 1
            if not _is_audio_file(remote):
                counts["unsupported"] += 1
                continue
            if _already_handled(db_path, remote):
                counts["already_known"] += 1
                continue

            recording_date = _recording_date(remote, source.kind)
            state = _backfill_state(recording_date, source.kind)
            if state != "eligible":
                _save_terminal_source(db_path, remote, source.kind, recording_date, state)
                counts[state] += 1
                continue
            if dry_run:
                counts["eligible"] += 1
                continue

            destination = _handoff_path(handoff_dir, remote)
            try:
                _stage_download(drive, remote, destination)
                _set_recording_timestamp(destination, remote.created_time)
                _save_staged_source(db_path, remote, source.kind, recording_date, destination)
                counts["downloaded"] += 1
            except Exception as exc:
                _save_failed_source(db_path, remote, source.kind, recording_date, str(exc))
                counts["failed"] += 1
    return counts


def configured_sources(db_path: Path | None = None) -> list[CaptureSource]:
    """Return enabled Drive folders without making Drive configuration mandatory."""
    sources: list[CaptureSource] = []
    if DRIVE_FUTURE_FOLDER_ID:
        sources.append(CaptureSource(DRIVE_FUTURE_FOLDER_ID, "future"))
    if DRIVE_BACKFILL_FOLDER_ID and not _backfill_complete(db_path):
        sources.append(CaptureSource(DRIVE_BACKFILL_FOLDER_ID, "backfill"))
    return sources


def complete_backfill(db_path: Path | None = None) -> None:
    """Disable the one-time backfill only after every staged file is ingested."""
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS count FROM drive_sources "
            "WHERE folder_kind = 'backfill' AND state NOT IN ('ingested', 'excluded')"
        ).fetchone()["count"]
        if pending:
            raise CaptureError(f"{pending} backfill file(s) still need ingesting")
        db.set_setting(conn, "drive_backfill_complete", "1")


def reconcile_ingested(
    *, db_path: Path | None = None, handoff_dir: Path | None = None
) -> int:
    """Link staged Drive files to manifest rows and remove local handoff copies."""
    db.init_db(db_path)
    removable_root = (handoff_dir or DRIVE_HANDOFF_DIR).resolve()
    linked = 0
    with db.connect(db_path) as conn:
        for source in db.staged_drive_sources(conn):
            if not source.local_path:
                continue
            local_path = Path(source.local_path)
            meeting = conn.execute(
                "SELECT id FROM meetings WHERE source_path = ?", (source.local_path,)
            ).fetchone()
            if not meeting and local_path.is_file():
                meeting = conn.execute(
                    "SELECT id FROM meetings WHERE id = ?", (ingest.hash_file(local_path),)
                ).fetchone()
            if not meeting:
                continue
            _remove_handoff_file(local_path, removable_root)
            db.link_drive_source_to_meeting(
                conn, source.drive_file_id, source.drive_version, meeting["id"]
            )
            linked += 1
    return linked


def rehydrate_audio(
    meeting: db.Meeting,
    *,
    client: DriveClient | None = None,
    db_path: Path | None = None,
) -> Path:
    """Restore a previously transcribed Drive recording only when it is unchanged."""
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        source = db.get_drive_source_for_meeting(conn, meeting.id)
    if not source:
        raise CaptureError("no Drive source is recorded for this meeting")

    drive = client or google_drive_client()
    current = drive.get_file(source.drive_file_id)
    if current.version != source.drive_version:
        raise CaptureError("Drive source changed; refusing to transcribe different audio")

    destination = _archive_path(meeting, current)
    _stage_download(drive, current, destination)
    with db.connect(db_path) as conn:
        db.advance(conn, meeting.id, meeting.status, audio_path=str(destination))
    return destination


def cleanup_transcribed_audio(
    meeting_id: str, audio_path: str, *, db_path: Path | None = None
) -> bool:
    """Free local Drive-backed audio only after transcription is safely committed."""
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        if not db.get_drive_source_for_meeting(conn, meeting_id):
            return False
    path = Path(audio_path)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise CaptureError(f"could not remove local audio archive: {exc}") from exc
    with db.connect(db_path) as conn:
        meeting = db.get_meeting(conn, meeting_id)
        if meeting:
            db.advance(conn, meeting_id, meeting.status, audio_path=None)
    return True


def google_drive_client() -> DriveClient:
    """Build the real API client lazily so core pipeline commands need no SDK import."""
    credentials = _credentials(interactive=False)
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise CaptureError("Drive dependencies are missing; run `uv sync`") from exc
    return _GoogleDriveClient(build("drive", "v3", credentials=credentials), MediaIoBaseDownload)


def authorize() -> None:
    """Authorize Google Drive interactively via browser OAuth."""
    _credentials(interactive=True)
    print("Google Drive successfully authorized.")


def _credentials(*, interactive: bool):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise CaptureError("Drive dependencies are missing; run `uv sync`") from exc

    credentials = None
    if DRIVE_TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(DRIVE_TOKEN_FILE), DRIVE_SCOPES)
        except Exception:
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            credentials = None
    if not credentials or not credentials.valid:
        if not interactive:
            raise CaptureError("Drive is not authorized or token expired; run `python -m pipeline.cli auth-drive` once")
        if not DRIVE_CREDENTIALS_FILE.exists():
            raise CaptureError(f"OAuth desktop client file is missing: {DRIVE_CREDENTIALS_FILE}")
        credentials = InstalledAppFlow.from_client_secrets_file(
            str(DRIVE_CREDENTIALS_FILE), DRIVE_SCOPES
        ).run_local_server(port=0)
    if credentials and credentials.valid:
        DRIVE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        DRIVE_TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


class _GoogleDriveClient:
    """Drive API adapter; all policy remains in the functions above."""

    def __init__(self, service, downloader_type) -> None:
        self._service = service
        self._downloader_type = downloader_type

    def list_files(self, folder_id: str) -> list[DriveFile]:
        files: list[DriveFile] = []
        page_token = None
        while True:
            response = self._service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive",
                fields=(
                    "nextPageToken,files(id,version,name,mimeType,size,md5Checksum,"
                    "createdTime,modifiedTime,webViewLink)"
                ),
                orderBy="createdTime",
                pageToken=page_token,
            ).execute()
            files.extend(_drive_file(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    def get_file(self, file_id: str) -> DriveFile:
        item = self._service.files().get(
            fileId=file_id,
            fields="id,version,name,mimeType,size,md5Checksum,createdTime,modifiedTime,webViewLink",
        ).execute()
        return _drive_file(item)

    def download(self, file_id: str, destination: Path) -> None:
        from io import FileIO

        request = self._service.files().get_media(fileId=file_id)
        with FileIO(destination, "wb") as handle:
            downloader = self._downloader_type(handle, request)
            complete = False
            while not complete:
                _, complete = downloader.next_chunk()


def _drive_file(item: dict[str, object]) -> DriveFile:
    return DriveFile(
        file_id=str(item["id"]),
        version=str(item.get("version", "")),
        name=str(item["name"]),
        mime_type=_optional_text(item.get("mimeType")),
        byte_size=int(item["size"]) if item.get("size") is not None else None,
        md5_checksum=_optional_text(item.get("md5Checksum")),
        created_time=_optional_text(item.get("createdTime")),
        modified_time=_optional_text(item.get("modifiedTime")),
        web_view_link=_optional_text(item.get("webViewLink")),
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _empty_counts() -> dict[str, int]:
    return {
        "scanned": 0,
        "downloaded": 0,
        "eligible": 0,
        "already_known": 0,
        "unsupported": 0,
        "excluded": 0,
        "ambiguous": 0,
        "failed": 0,
    }


def _is_audio_file(remote: DriveFile) -> bool:
    return Path(remote.name).suffix.lower() in ingest.AUDIO_EXTENSIONS


def _already_handled(db_path: Path | None, remote: DriveFile) -> bool:
    with db.connect(db_path) as conn:
        source = db.get_drive_source(conn, remote.file_id, remote.version)
    return source is not None and source.state != "failed"


def _recording_date(remote: DriveFile, folder_kind: str) -> str | None:
    if folder_kind == "backfill":
        return ingest.parse_explicit_filename_date(
            Path(remote.name), int(DRIVE_BACKFILL_CUTOFF[:4])
        )
    timestamp = _parse_time(remote.created_time)
    fallback_year = timestamp.year if timestamp else datetime.now(TZ).year
    parsed = ingest.parse_explicit_filename_date(Path(remote.name), fallback_year)
    return parsed or _date_from(timestamp)


def _backfill_state(recording_date: str | None, folder_kind: str) -> str:
    if folder_kind != "backfill":
        return "eligible"
    if not recording_date:
        return "ambiguous"
    return "eligible" if recording_date >= DRIVE_BACKFILL_CUTOFF else "excluded"


def _save_terminal_source(
    db_path: Path | None,
    remote: DriveFile,
    folder_kind: str,
    recording_date: str | None,
    state: str,
) -> None:
    _save_source(db_path, remote, folder_kind, recording_date, state=state)


def _save_staged_source(
    db_path: Path | None,
    remote: DriveFile,
    folder_kind: str,
    recording_date: str | None,
    destination: Path,
) -> None:
    _save_source(
        db_path, remote, folder_kind, recording_date, state="staged", local_path=str(destination)
    )


def _save_failed_source(
    db_path: Path | None,
    remote: DriveFile,
    folder_kind: str,
    recording_date: str | None,
    error: str,
) -> None:
    _save_source(db_path, remote, folder_kind, recording_date, state="failed", error=error[:4000])


def _save_source(
    db_path: Path | None,
    remote: DriveFile,
    folder_kind: str,
    recording_date: str | None,
    *,
    state: str,
    local_path: str | None = None,
    error: str | None = None,
) -> None:
    with db.connect(db_path) as conn:
        db.upsert_drive_source(
            conn,
            drive_file_id=remote.file_id,
            drive_version=remote.version,
            folder_kind=folder_kind,
            source_name=remote.name,
            mime_type=remote.mime_type,
            byte_size=remote.byte_size,
            md5_checksum=remote.md5_checksum,
            created_time=remote.created_time,
            modified_time=remote.modified_time,
            web_view_link=remote.web_view_link,
            recording_date=recording_date,
            state=state,
            local_path=local_path,
            error=error,
        )


def _handoff_path(handoff_dir: Path, remote: DriveFile) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(remote.name).name)
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", remote.version) or "unknown"
    return handoff_dir / f"{remote.file_id}_{safe_version}_{safe_name}"


def _archive_path(meeting: db.Meeting, remote: DriveFile) -> Path:
    suffix = Path(remote.name).suffix.lower()
    date = meeting.meeting_date or "unknown-date"
    return AUDIO_DIR / f"{date}_{meeting.id[:12]}{suffix}"


def _stage_download(client: DriveClient, remote: DriveFile, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        client.download(remote.file_id, temporary)
        _verify_download(temporary, remote)
        if destination.exists():
            with contextlib.suppress(Exception):
                destination.unlink(missing_ok=True)
        for attempt in range(5):
            try:
                shutil.move(str(temporary), str(destination))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _verify_download(path: Path, remote: DriveFile) -> None:
    size = path.stat().st_size
    if remote.byte_size is not None and size != remote.byte_size:
        raise CaptureError(f"download size mismatch: expected {remote.byte_size}, got {size}")
    if remote.md5_checksum and _md5(path) != remote.md5_checksum:
        raise CaptureError("download checksum mismatch")


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_recording_timestamp(path: Path, created_time: str | None) -> None:
    timestamp = _parse_time(created_time)
    if timestamp:
        seconds = timestamp.timestamp()
        os.utime(path, (seconds, seconds))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(TZ)
    except ValueError:
        return None


def _date_from(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d") if value else None


def _backfill_complete(db_path: Path | None) -> bool:
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        return db.get_setting(conn, "drive_backfill_complete") == "1"


def _remove_handoff_file(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise CaptureError(f"refusing to remove a file outside the Drive handoff: {path}")
    resolved.unlink(missing_ok=True)

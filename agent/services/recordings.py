"""Twilio call recording -> Google Drive pipeline.

Flow: Twilio finishes recording a call -> POSTs to the /recording-status
webhook (agent/views.py::recording_status) -> process_recording() here
downloads the audio from Twilio, uploads it to a folder inside a Google
Shared Drive, deletes it from Twilio once the upload is confirmed, and
records the result on the matching Recording row. Playback later reads the
same Drive file back through stream_recording_bytes(), never a public link.

Uploads authenticate as a real Google user (via OAuth, see
agent/views.py::google_drive_authorize/google_drive_callback), not a
service account -- service accounts have no storage quota of their own and
can't write into regular Drive storage. The refresh token from that one-
time consent flow is stored in the GoogleDriveAuth singleton row and reused
here to mint fresh access tokens as needed.
"""

import asyncio
import io
import os

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from loguru import logger
from twilio.rest import Client as TwilioClient

from agent.models import CallLog, GoogleDriveAuth, Recording

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


class DriveNotAuthorized(Exception):
    """Raised when no Google Drive OAuth token has been set up yet -- the
    admin needs to visit /google-drive/authorize first."""


def _get_drive_service():
    """Build the Drive API client from the stored OAuth refresh token.
    googleapiclient is sync-only, so every call through this client must be
    wrapped in asyncio.to_thread by the caller. Rebuilt on every call
    (cheap) rather than cached, since the stored refresh token can change
    (re-authorizing replaces it) and Credentials auto-refreshes the access
    token internally as needed anyway.
    """
    auth_row = GoogleDriveAuth.objects.first()
    if auth_row is None:
        raise DriveNotAuthorized(
            "Google Drive isn't connected yet -- an admin needs to visit "
            "/google-drive/authorize once to grant access."
        )
    creds = Credentials(
        token=None,
        refresh_token=auth_row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_DRIVE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_DRIVE_CLIENT_SECRET"),
        scopes=DRIVE_SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def _download_from_twilio(recording_sid: str) -> bytes:
    """Pull the recording's audio bytes from Twilio's REST media endpoint
    (Basic Auth with the account SID/token -- same credentials used
    everywhere else in this project for the Twilio REST API)."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"
    auth = aiohttp.BasicAuth(account_sid, auth_token)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, auth=auth) as resp:
            resp.raise_for_status()
            return await resp.read()


def _upload_to_drive_sync(filename: str, content: bytes) -> str:
    """Blocking Drive upload -- call via asyncio.to_thread. Returns the new
    file's Drive file id. supportsAllDrives=True is required for every call
    that touches a file living inside a Shared Drive."""
    drive_service = _get_drive_service()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="audio/mpeg", resumable=False)
    file = (
        drive_service.files()
        .create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return file["id"]


def _delete_twilio_recording_sync(recording_sid: str):
    """Blocking Twilio recording delete -- call via asyncio.to_thread."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    client = TwilioClient(account_sid, auth_token)
    client.recordings(recording_sid).delete()


def _download_from_drive_sync(file_id: str) -> bytes:
    """Blocking Drive download -- call via asyncio.to_thread. Used by the
    admin playback endpoint, never exposed as a public Drive link."""
    drive_service = _get_drive_service()
    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


async def process_recording(call_sid: str, recording_sid: str, duration_seconds: int | None):
    """The full pipeline, run from the /recording-status webhook once
    Twilio reports a recording as completed. Only deletes the Twilio copy
    once the Drive upload is confirmed -- a failed upload leaves the
    recording sitting on Twilio rather than losing it outright."""
    call_log = await CallLog.objects.filter(call_sid=call_sid).afirst()
    if call_log is None:
        logger.warning(f"process_recording: no CallLog for call_sid={call_sid!r}, skipping")
        return

    recording, _ = await Recording.objects.aupdate_or_create(
        twilio_recording_sid=recording_sid,
        defaults={"call_log": call_log, "duration_seconds": duration_seconds, "status": Recording.STATUS_PENDING},
    )

    try:
        content = await _download_from_twilio(recording_sid)
        gdrive_file_id = await asyncio.to_thread(
            _upload_to_drive_sync, f"{call_sid}_{recording_sid}.mp3", content
        )
        await asyncio.to_thread(_delete_twilio_recording_sync, recording_sid)
    except Exception as exc:  # noqa: BLE001 -- record the failure, don't crash the webhook
        logger.error(f"process_recording: failed for {recording_sid}: {exc}")
        recording.status = Recording.STATUS_FAILED
        recording.error = str(exc)
        await recording.asave()
        return

    recording.status = Recording.STATUS_UPLOADED
    recording.gdrive_file_id = gdrive_file_id
    await recording.asave()
    logger.info(f"process_recording: {recording_sid} uploaded to Drive as {gdrive_file_id}, deleted from Twilio")


async def stream_recording_bytes(gdrive_file_id: str) -> bytes:
    return await asyncio.to_thread(_download_from_drive_sync, gdrive_file_id)

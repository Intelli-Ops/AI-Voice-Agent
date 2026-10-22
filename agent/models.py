from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class Profile(models.Model):
    """Extends Django's built-in User with the one extra thing we need:
    which of the two roles (admin/user) they have. A OneToOne profile is
    used instead of a custom User model since auth.User already covers
    everything else (username, password, login) and swapping the user
    model after the fact is painful -- this keeps that door open.
    """

    ROLE_ADMIN = "admin"
    ROLE_USER = "user"
    ROLE_CHOICES = [(ROLE_ADMIN, "Admin"), (ROLE_USER, "User")]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_USER)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


@receiver(post_save, sender=User)
def _create_profile_for_new_user(sender, instance, created, **kwargs):
    """Every User needs a Profile to have a role -- auto-create one (as
    the default 'user' role) whenever a new User is saved, so creating an
    account via createsuperuser/the admin site never leaves it without one.
    """
    if created:
        Profile.objects.get_or_create(user=instance)


class CallBatch(models.Model):
    """One "Call All Rows" run over an uploaded claims sheet. Its rows are
    CallLog objects, created up front (queued) and dialed one after another
    by agent/services/batches.py.
    """

    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_STOPPED = "stopped"  # a user pressed Stop Batch
    STATUS_INTERRUPTED = "interrupted"  # the server restarted mid-batch
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_INTERRUPTED, "Interrupted"),
    ]

    # Shown to people as "Batch 1, 2, 3...". Not the database id, which also
    # counts batches created and deleted by test runs.
    number = models.PositiveIntegerField(null=True, blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Batch {self.number} ({self.source_filename or 'no file name'}) -> {self.status}"


class CallLog(models.Model):
    """One row per real (Twilio) call placed -- who it went to, when, and
    how it ended, for the admin call-log view. The local mic demo isn't
    logged here since it isn't a real call to anyone.

    A batch creates its rows before dialing them, so a row can exist with
    no call_sid yet (queued) -- see status_label.
    """

    # Every finished call is either Verified (the rep confirmed the claim) or
    # Unverified, with call_outcome saying why. These are the stored values
    # and how they're shown:
    OUTCOME_VERIFIED = "verified"
    OUTCOME_CLAIM_NOT_CONFIRMED = "claim_not_confirmed"  # a person answered, claim never confirmed
    OUTCOME_IVR_DEAD_END = "ivr_dead_end_needs_callback"  # stuck in / ended in a phone menu
    OUTCOME_ENDED_EARLY = "ended_before_anyone_answered"  # connected, but no menu or person ever detected
    OUTCOME_CALLBACK = "callback_requested"  # asked us to call back another time
    OUTCOME_NOT_CALLED = "not_called"  # batch stopped/interrupted before reaching this row
    OUTCOME_LABELS = {
        OUTCOME_VERIFIED: "Verified",
        OUTCOME_CLAIM_NOT_CONFIRMED: "Claim not confirmed",
        OUTCOME_IVR_DEAD_END: "Stuck in phone menu",
        OUTCOME_ENDED_EARLY: "Ended before reaching anyone",
        OUTCOME_CALLBACK: "Asked to call back later",
        "no_answer": "No answer",
        "busy": "Line busy",
        "failed": "Call failed",
        "canceled": "Call canceled",
        OUTCOME_NOT_CALLED: "Not called",
        "error": "System error",
    }

    batch = models.ForeignKey(CallBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls")
    # Batch calls: the claim's row in the sheet, counted from 1 below the header
    # (row 1 = Excel row 2).
    row_number = models.PositiveIntegerField(null=True, blank=True)
    call_sid = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    destination_number = models.CharField(max_length=32)
    extension = models.CharField(max_length=16, blank=True)
    claim_context = models.JSONField(default=dict)
    # One of the OUTCOME_LABELS keys, or "" while the call is queued or still
    # in progress.
    call_outcome = models.CharField(max_length=32, blank=True)
    fields_captured = models.JSONField(default=dict)
    error = models.TextField(blank=True)  # why the call couldn't be placed, if it couldn't
    # Set on creation, then moved to the actual dial time for batch rows.
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    placed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def verification(self):
        """"verified", "unverified", or "" while there's no outcome yet."""
        if not self.call_outcome:
            return ""
        return "verified" if self.call_outcome == self.OUTCOME_VERIFIED else "unverified"

    @property
    def reason_label(self):
        """Why an unverified call is unverified ("No answer"...); blank otherwise."""
        if self.verification != "unverified":
            return ""
        return self.OUTCOME_LABELS.get(self.call_outcome, self.call_outcome.replace("_", " ").capitalize())

    @property
    def status_label(self):
        """Verified / Unverified, or Queued / In progress before there's an outcome."""
        if self.call_outcome:
            return self.verification.capitalize()
        return "In progress" if self.call_sid else "Queued"

    def __str__(self):
        return f"{self.destination_number} @ {self.started_at:%Y-%m-%d %H:%M} -> {self.status_label}"


class Recording(models.Model):
    """One row per Twilio call recording. The actual audio lives in Google
    Drive, not here and not on Twilio long-term -- see
    agent/services/recordings.py for the download -> upload -> delete
    pipeline that fills this in after Twilio's recording-status webhook
    fires. status tracks where a given recording is in that pipeline.
    """

    STATUS_PENDING = "pending"
    STATUS_UPLOADED = "uploaded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_FAILED, "Failed"),
    ]

    call_log = models.ForeignKey(CallLog, on_delete=models.CASCADE, related_name="recordings")
    twilio_recording_sid = models.CharField(max_length=64, unique=True, db_index=True)
    gdrive_file_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Recording {self.twilio_recording_sid} ({self.status})"


class GoogleDriveAuth(models.Model):
    """Singleton row holding the refresh token from the one-time Google
    OAuth consent flow (see /google-drive/authorize and /callback in
    agent/views.py). Service accounts can't write to regular Drive
    storage, so uploads act as this authorized user's own account instead
    -- the refresh token is what lets agent/services/recordings.py get a
    fresh access token on every call without repeating the consent screen.
    """

    refresh_token = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Google Drive OAuth token (updated {self.updated_at:%Y-%m-%d %H:%M})"

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


class CallLog(models.Model):
    """One row per real (Twilio) call placed -- who it went to, when, and
    how it ended, for the admin call-log view. The local mic demo isn't
    logged here since it isn't a real call to anyone.
    """

    call_sid = models.CharField(max_length=64, unique=True, db_index=True)
    destination_number = models.CharField(max_length=32)
    extension = models.CharField(max_length=16, blank=True)
    claim_context = models.JSONField(default=dict)
    # Mirrors main.py's save_call_outcome_if_unset values: success,
    # ivr_dead_end_needs_callback, no_answer, busy, failed, canceled, error,
    # or "" while the call is still in progress.
    call_outcome = models.CharField(max_length=32, blank=True)
    fields_captured = models.JSONField(default=dict)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    placed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.destination_number} @ {self.started_at:%Y-%m-%d %H:%M} -> {self.call_outcome or 'in progress'}"

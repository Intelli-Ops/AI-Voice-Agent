from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import CallLog, GoogleDriveAuth, Profile, Recording


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


class CustomUserAdmin(UserAdmin):
    inlines = [ProfileInline]


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = (
        "destination_number",
        "extension",
        "call_outcome",
        "started_at",
        "ended_at",
        "placed_by",
    )
    list_filter = ("call_outcome",)
    search_fields = ("destination_number", "call_sid")
    readonly_fields = [f.name for f in CallLog._meta.fields]


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ("twilio_recording_sid", "call_log", "status", "duration_seconds", "created_at")
    list_filter = ("status",)
    readonly_fields = [f.name for f in Recording._meta.fields]


@admin.register(GoogleDriveAuth)
class GoogleDriveAuthAdmin(admin.ModelAdmin):
    list_display = ("updated_at",)
    readonly_fields = ("refresh_token", "updated_at")

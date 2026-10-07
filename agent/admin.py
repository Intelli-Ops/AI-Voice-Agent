from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import CallLog, Profile


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

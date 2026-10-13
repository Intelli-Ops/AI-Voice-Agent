from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", auth_views.LoginView.as_view(template_name="agent/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("call-logs", views.admin_call_logs, name="call-logs"),
    path("google-drive/authorize", views.google_drive_authorize, name="google-drive-authorize"),
    # Path must match GOOGLE_DRIVE_REDIRECT_URI's path exactly -- this is
    # what's registered in the Google Cloud Console OAuth client.
    path("callback", views.google_drive_callback, name="google-drive-callback"),
    path("api/start-call", views.start_call, name="start-call"),
    path("api/end-call", views.end_call, name="end-call"),
    path("api/place-call", views.place_call, name="place-call"),
    path("twiml", views.twiml, name="twiml"),
    path("call-status", views.call_status, name="call-status"),
    path("recording-status", views.recording_status, name="recording-status"),
    path("api/recordings/<int:recording_id>/stream", views.stream_recording, name="stream-recording"),
    path("api/results", views.get_results, name="results"),
    path("api/upload-claims", views.upload_claims, name="upload-claims"),
    path("api/claims", views.get_claims, name="claims"),
]

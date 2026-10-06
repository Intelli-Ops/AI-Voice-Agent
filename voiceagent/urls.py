from django.urls import include, path

urlpatterns = [
    path("", include("agent.urls")),
]

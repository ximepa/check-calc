"""URL configuration for the checkcalc project.

The project is an admin-only application, so the root simply forwards to it.
"""

from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False), name="home"),
]

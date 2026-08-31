"""URL configuration for the checkcalc project.

The project is an admin-only application, so the root simply forwards to it.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False), name="home"),
]

if settings.DEBUG:
    # Serve uploaded receipt photos in development; a real deployment puts
    # MEDIA_ROOT behind the web server instead.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from django.shortcuts import redirect
from django.conf import settings
from django.conf.urls.static import static
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

from accounts.views import healthz


# Throttle the login form to 5 POSTs/min/IP — blocks credential stuffing
# without burning a real user who mistypes once. Anonymous GETs are unlimited.
RateLimitedLoginView = method_decorator(
    ratelimit(key='ip', rate='5/m', method='POST', block=True),
    name='post',
)(auth_views.LoginView)


urlpatterns = [
    path('', lambda request: redirect('accounts:dashboard')),
    path('healthz', healthz, name='healthz'),
    path('admin/', admin.site.urls),
    path('accounts/login/', RateLimitedLoginView.as_view(), name='login'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('accounts/', include('accounts.urls')),
    path('groups/', include('groups.urls')),
    path('channels/', include('channels.urls')),
    path('jobs/', include('jobs.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

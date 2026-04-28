from django.urls import path
from . import views

app_name = 'channels'

urlpatterns = [
    path('', views.channel_list, name='list'),
]

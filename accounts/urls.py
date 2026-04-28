from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('profile/', views.profile, name='profile'),
    path('add/', views.initiate_telethon_login, name='add_account'),
    path('verify/', views.verify_telethon_login, name='verify_login'),
    path('cancel-login/', views.cancel_login, name='cancel_login'),
    path('relogin/<int:pk>/', views.relogin_account, name='relogin_account'),
    path('edit/<int:pk>/', views.edit_account, name='edit_account'),
    path('detail/<int:pk>/', views.account_detail, name='account_detail'),
    path('tags/', views.tag_list, name='tag_list'),
    path('proxies/', views.proxy_list, name='proxy_list'),
    path('proxies/<int:pk>/', views.proxy_detail, name='proxy_detail'),
]



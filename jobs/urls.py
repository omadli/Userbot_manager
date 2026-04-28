from django.urls import path

from . import views

app_name = 'jobs'

urlpatterns = [
    # Name pools
    path('pools/', views.pool_list, name='pool_list'),
    path('pools/<int:pk>/', views.pool_detail, name='pool_detail'),

    # Tasks
    path('', views.task_list, name='task_list'),
    path('stats/', views.stats_dashboard, name='stats_dashboard'),
    path('create-groups/', views.task_create_groups, name='task_create_groups'),
    path('create-channels/', views.task_create_channels, name='task_create_channels'),
    path('join-channel/', views.task_create_join_channel, name='task_create_join_channel'),
    path('leave-groups/',   views.task_create_leave_chats, {'kind': 'group'},
         name='task_create_leave_groups'),
    path('leave-channels/', views.task_create_leave_chats, {'kind': 'channel'},
         name='task_create_leave_channels'),
    path('boost-views/', views.task_create_boost_views, name='task_create_boost_views'),
    path('react/', views.task_create_react_to_post, name='task_create_react_to_post'),
    path('vote-poll/', views.task_create_vote_poll, name='task_create_vote_poll'),
    path('press-start/', views.task_create_press_start, name='task_create_press_start'),
    path('warming/', views.task_create_account_warming, name='task_create_account_warming'),

    # Admin-only scripts
    path('scripts/', views.script_list, name='script_list'),
    path('scripts/<int:pk>/', views.script_detail, name='script_detail'),
    path('run-script/', views.task_create_run_script, name='task_create_run_script'),

    path('<int:pk>/', views.task_detail, name='task_detail'),

    # JSON (AJAX polling)
    path('<int:pk>/progress/', views.task_progress_json, name='task_progress'),
    path('<int:pk>/events/', views.task_events_json, name='task_events'),
]

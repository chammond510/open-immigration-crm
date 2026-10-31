from django.urls import path

from . import views

urlpatterns = [
    path("", views.notification_list, name="notification_list"),
    path("read/", views.notifications_read, name="notifications_read"),
    path("<int:pk>/", views.notification_open, name="notification_open"),
]

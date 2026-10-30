from django.urls import path

from . import views

urlpatterns = [path("", views.operating_dashboard, name="operating_dashboard")]

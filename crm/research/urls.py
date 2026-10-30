from django.urls import path

from . import views

urlpatterns = [
    path("matters/<uuid:pk>/search/", views.search, name="research_search"),
    path("matters/<uuid:pk>/authorities/new/", views.add_by_hand, name="research_add_by_hand"),
    path("searches/<int:pk>/", views.search_detail, name="research_search_detail"),
    path("searches/<int:pk>/save/", views.save, name="research_save"),
    path("authorities/<int:pk>/remove/", views.remove, name="research_remove"),
]

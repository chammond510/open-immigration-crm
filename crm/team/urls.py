from django.urls import path

from . import views

urlpatterns = [
    path("", views.team_list, name="team_list"),
    path("add/", views.person_add, name="team_add"),
    path("<int:pk>/", views.person_detail, name="team_detail"),
    path("<int:pk>/invite/", views.person_invite, name="team_invite"),
    path(
        "<int:pk>/two-factor/reset/",
        views.person_two_factor_reset,
        name="team_two_factor_reset",
    ),
    path("<int:pk>/deactivate/", views.person_deactivate, name="team_deactivate"),
    path("<int:pk>/reactivate/", views.person_reactivate, name="team_reactivate"),
]

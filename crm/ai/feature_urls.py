from django.urls import path

from . import feature_views as views

urlpatterns = [
    path("assistant/", views.assistant_home, name="ai_assistant"),
    path("assistant/new/", views.assistant_start, name="ai_assistant_start"),
    path("assistant/<uuid:pk>/", views.conversation_detail, name="ai_conversation"),
    path("assistant/<uuid:pk>/send/", views.conversation_send, name="ai_conversation_send"),
    path("assistant/<uuid:pk>/status/", views.conversation_status, name="ai_conversation_status"),
    path("assistant/<uuid:pk>/delete/", views.conversation_delete, name="ai_conversation_delete"),
    path("assistant/changes/<int:pk>/", views.conversation_decide, name="ai_conversation_decide"),
    path("consults/<int:pk>/memo/", views.consult_memo_prepare, name="ai_consult_memo_prepare"),
    path(
        "consults/<int:pk>/memo/status/",
        views.consult_memo_status,
        name="ai_consult_memo_status",
    ),
    path("suggestions/<int:pk>/accept/", views.suggestion_accept, name="ai_suggestion_accept"),
    path("suggestions/<int:pk>/dismiss/", views.suggestion_dismiss, name="ai_suggestion_dismiss"),
]

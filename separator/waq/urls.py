from django.urls import path

from .api import views

app_name = "waq"

urlpatterns = [
    path("lead/", views.lead, name="lead"),
]

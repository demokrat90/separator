from django.urls import path

from .api import views

app_name = "attribution"

urlpatterns = [
    path("click/", views.click, name="click"),
    path("health/", views.health, name="health"),
]

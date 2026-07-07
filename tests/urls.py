from django.urls import include, path

urlpatterns = [
    path("oura/", include("oura.urls")),
]

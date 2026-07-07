from django.contrib import admin

from .models import OuraConnection


@admin.register(OuraConnection)
class OuraConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "oura_user_id",
        "status",
        "connected_at",
        "last_sync_at",
    )
    list_filter = ("status",)
    search_fields = ("customer__username", "oura_user_id")
    readonly_fields = ("connected_at",)

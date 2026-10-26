from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.http import HttpRequest

from apps.accounts.models import SecurityToken, User

# Fields that decide what a staff member is allowed to do. Only superusers may change them,
# otherwise anyone who can edit users could make themselves a superuser.
PRIVILEGE_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = [
        "email",
        "first_name",
        "last_name",
        "auth_method",
        "email_verified",
        "is_active",
        "account_expires_at",
        "date_joined",
    ]
    list_filter = ["auth_method", "email_verified", "is_active", "is_staff"]
    search_fields = ["email", "first_name", "last_name"]
    ordering = ["-date_joined"]
    readonly_fields = [
        "date_joined",
        "last_login",
        "google_id",
        "marketing_email_opt_in_at",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal details", {"fields": ("first_name", "last_name", "phone", "postcode", "photo")}),
        (
            "Learning profile",
            {
                "fields": ("theory_test_date", "confidence_level"),
                "description": "What the learner told us about their test. They fill this in themselves.",
            },
        ),
        (
            "Marketing",
            {
                "fields": ("marketing_email_opt_in", "marketing_email_opt_in_at"),
                "description": "Consent the learner gave themselves. Do not tick it on their behalf.",
            },
        ),
        ("Sign-in", {"fields": ("auth_method", "google_id", "email_verified")}),
        ("Access", {"fields": ("is_active", "account_expires_at")}),
        ("Staff permissions", {"fields": PRIVILEGE_FIELDS}),
        ("Dates", {"fields": ("date_joined", "last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = ((None, {"fields": ("email", "first_name", "password1", "password2")}),)

    def get_fieldsets(self, request: HttpRequest, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and not request.user.is_superuser:
            # Hide the section entirely rather than showing fields that cannot be saved.
            return tuple(section for section in fieldsets if section[0] != "Staff permissions")
        return fieldsets

    def get_readonly_fields(self, request: HttpRequest, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            # Hiding the fields is not enough: a hand-written POST would still submit them.
            readonly += list(PRIVILEGE_FIELDS)
        return readonly


@admin.register(SecurityToken)
class SecurityTokenAdmin(admin.ModelAdmin):
    """Read-only view of outstanding verification and password reset links, for support queries."""

    list_display = ["purpose", "user", "created_at", "expires_at", "used_at"]
    list_filter = ["purpose"]
    search_fields = ["user__email"]
    ordering = ["-created_at"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

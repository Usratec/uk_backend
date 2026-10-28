from django.db import models


class Dashboard(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = [("view_dashboard", "Can view the business dashboard")]
        verbose_name = "Business dashboard"
        verbose_name_plural = "Business dashboard"

    def __str__(self) -> str:
        return "Business dashboard"

from django.db import models


class InventoryPermission(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ('view_inventory', 'Can view inventory dashboard'),
            ('manage_inventory', 'Can manage inventory dashboard'),
        )
        verbose_name = 'inventory permission'
        verbose_name_plural = 'inventory permissions'

#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType

# Get the jivo user
user = User.objects.get(username='jivo')

# Verify current state
print("=== Current User Status ===")
print(f"Username: {user.username}")
print(f"Is Active: {user.is_active}")
print(f"Is Staff: {user.is_staff}")
print(f"Is Superuser: {user.is_superuser}")
print(f"User permissions count: {user.user_permissions.count()}")
print(f"Group count: {user.groups.count()}")

# Make sure user is active, staff, and superuser
user.is_active = True
user.is_staff = True
user.is_superuser = True
user.save()

print("\n=== Updated User Status ===")
print(f"Is Active: {user.is_active}")
print(f"Is Staff: {user.is_staff}")
print(f"Is Superuser: {user.is_superuser}")

# Grant all permissions
all_permissions = Permission.objects.all()
user.user_permissions.set(all_permissions)

print(f"\n=== Permissions Granted ===")
print(f"Total permissions assigned: {user.user_permissions.count()}")

# Verify
user = User.objects.get(username='jivo')  # Refresh
print(f"\n✓ User '{user.username}' is now fully configured!")
print(f"✓ Can access Django admin: YES")
print(f"✓ Permissions: ALL ({user.user_permissions.count()} permissions)")

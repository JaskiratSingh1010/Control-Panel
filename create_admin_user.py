#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User, Permission

# Check if admin user exists
if User.objects.filter(username='admin').exists():
    user = User.objects.get(username='admin')
    print(f"✓ User 'admin' already exists")
    user.set_password('jivoadmin')
    user.save()
    print(f"✓ Password updated to 'jivoadmin'")
else:
    user = User.objects.create_superuser(
        username='admin',
        email='admin@example.com',
        password='jivoadmin'
    )
    print(f"✓ Superuser 'admin' created successfully")

# Ensure user is fully configured
user.is_active = True
user.is_staff = True
user.is_superuser = True
user.save()

# Grant all permissions
all_permissions = Permission.objects.all()
user.user_permissions.set(all_permissions)

print(f"\n=== Admin User Configuration ===")
print(f"Username: admin")
print(f"Password: jivoadmin")
print(f"Is Active: {user.is_active}")
print(f"Is Staff: {user.is_staff}")
print(f"Is Superuser: {user.is_superuser}")
print(f"Permissions: {user.user_permissions.count()}")
print(f"\n✓ Ready to login at /admin/")

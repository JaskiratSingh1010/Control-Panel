#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User

# List all users
print("=== Users in Database ===")
for user in User.objects.all():
    print(f"\nUsername: {user.username}")
    print(f"  Email: {user.email}")
    print(f"  Is Active: {user.is_active}")
    print(f"  Is Staff: {user.is_staff}")
    print(f"  Is Superuser: {user.is_superuser}")
    print(f"  Permissions: {user.user_permissions.count()}")
    print(f"  Groups: {user.groups.count()}")
    
    # Test password
    if user.username in ['admin', 'jivo']:
        is_valid = user.check_password('jivoadmin')
        print(f"  Password 'jivoadmin' valid: {is_valid}")

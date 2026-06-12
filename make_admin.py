#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User

user = User.objects.get(username='jivo')
user.is_superuser = True
user.is_staff = True
user.save()
print(f"✓ User updated successfully!")
print(f"✓ Username: {user.username}")
print(f"✓ Is superuser: {user.is_superuser}")
print(f"✓ Is staff: {user.is_staff}")
print(f"\nYou can now login to Django admin with:")
print(f"  Username: jivo")
print(f"  Password: jivoadmin")

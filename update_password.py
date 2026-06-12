#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User

user = User.objects.get(username='jivo')
user.set_password('jivoadmin')
user.save()
print(f"✓ Password updated for user: {user.username}")
print(f"✓ Email: {user.email}")
print(f"✓ Is superuser: {user.is_superuser}")
print(f"✓ Is staff: {user.is_staff}")

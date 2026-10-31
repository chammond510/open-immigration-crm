from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from crm.models import Contact, Matter


def make_user(username="attorney", *, superuser=True, role=None):
    """A superuser by default; staff with only everyday work, or with ``role``, otherwise."""
    User = get_user_model()
    if superuser and role is None:
        return User.objects.create_superuser(
            username=username,
            email=f"{username}@example.test",
            password="Testing-Password-Only-42!",
        )
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.test",
        password="Testing-Password-Only-42!",
        is_staff=True,
    )
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


def make_contact(owner=None, *, first_name="Amina", last_name="Example", **kwargs):
    return Contact.objects.create(
        first_name=first_name,
        last_name=last_name,
        email=kwargs.pop("email", f"{first_name.lower()}@example.test"),
        assigned_to=owner,
        **kwargs,
    )


def make_matter(contact, owner=None, *, title="Fictional family matter", **kwargs):
    return Matter.objects.create(
        primary_contact=contact,
        title=title,
        assigned_to=owner,
        **kwargs,
    )

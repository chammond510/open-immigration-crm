import json

from django.test import TestCase

from crm.api.models import ApiSettings, ApiToken
from crm.api.scopes import ALL_SCOPES
from crm.tests.factories import make_user


class ApiTestCase(TestCase):
    def setUp(self):
        ApiSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
        self.user = make_user()

    def issue(self, *scopes, user=None, lifetime_days=30):
        token, raw_token = ApiToken.issue(
            user=user or self.user,
            name="Fictional automation",
            scopes=scopes or ALL_SCOPES,
            lifetime_days=lifetime_days,
        )
        return token, raw_token

    def api(self, method, path, data=None, *, token=None, **kwargs):
        if token is None:
            token = getattr(self, "raw_token", None) or self.issue()[1]
            self.raw_token = token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        call = getattr(self.client, method.lower())
        if data is not None:
            kwargs.setdefault("content_type", "application/json")
            data = json.dumps(data) if kwargs["content_type"] == "application/json" else data
            return call(path, data, headers=headers, **kwargs)
        return call(path, headers=headers, **kwargs)

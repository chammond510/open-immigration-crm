from django.conf import settings
from django.db import models


class Search(models.Model):
    """One search a staff member ran from a matter, and the results it returned."""

    class Status(models.TextChoices):
        DONE = "done", "Done"
        PREVIEWED = "previewed", "Not sent: preview mode"
        FAILED = "failed", "Did not finish"

    matter = models.ForeignKey(
        "crm.Matter", on_delete=models.CASCADE, related_name="research_searches"
    )
    connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source = models.CharField(max_length=120, blank=True)
    query = models.CharField(max_length=500)
    scope = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    # The results as the connector returned them, in core's shape; see services.RESULT_FIELDS.
    results = models.JSONField(default=list, blank=True)
    total = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    searched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "searches"

    def __str__(self):
        return self.query


class Authority(models.Model):
    """A legal authority saved to a matter from a search."""

    matter = models.ForeignKey("crm.Matter", on_delete=models.CASCADE, related_name="authorities")
    connection = models.ForeignKey(
        "connect.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # The service it came from, and its ID there, so it is saved only once per matter.
    source = models.CharField(max_length=120, blank=True)
    connector_key = models.CharField(max_length=41)
    external_id = models.CharField(max_length=255)
    citation = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=500)
    court = models.CharField(max_length=255, blank=True)
    decided = models.DateField(null=True, blank=True)
    kind = models.CharField(max_length=120, blank=True)
    # The service's own note on its standing, such as precedential or overruled.
    standing = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1000, blank=True)
    excerpt = models.TextField(blank=True)
    note = models.TextField(blank=True)
    query = models.CharField(max_length=500, blank=True)
    saved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "authorities"
        constraints = [
            models.UniqueConstraint(
                fields=["matter", "connector_key", "external_id"], name="authority_once_per_matter"
            )
        ]

    def __str__(self):
        return self.citation or self.title

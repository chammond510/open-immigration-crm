from django.core.checks import Warning, register

from .registry import registry


@register()
def connector_load_check(app_configs, **kwargs):
    return [
        Warning(
            f"A connector could not be loaded and is unavailable: {error}",
            id="connect.W001",
        )
        for error in registry.load_errors
    ]

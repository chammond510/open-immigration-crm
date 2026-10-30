from django.apps import AppConfig


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm.mcp"
    label = "mcp"
    verbose_name = "MCP server"

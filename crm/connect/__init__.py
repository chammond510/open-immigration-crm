"""Provider-neutral connector framework.

Core code talks to capabilities such as "phone" or "payments". Provider
adapters live in the top-level ``connectors`` package or in installed
packages, and core never imports them directly; the registry loads them.
"""

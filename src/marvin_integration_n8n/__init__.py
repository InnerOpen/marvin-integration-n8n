"""n8n integration for Marvin — trigger n8n workflows, pick them from n8n's list, read executions back."""

from .provider import N8nProvider

__all__ = ["N8nProvider"]
__version__ = "0.4.0"

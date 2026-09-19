"""Native standalone entrypoint for the Jev Hermes plugin."""

from .hermes_switchyard import register

__all__ = ["register"]

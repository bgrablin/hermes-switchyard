"""Native standalone entrypoint for the Jev Hermes plugin."""

from .jev_decision import register

__all__ = ["register"]

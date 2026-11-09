"""Command-line front end (`jap r ...`). Shares all watch logic with the UI via jap.restaurants.watch."""

from .main import register, run

__all__ = ["register", "run"]

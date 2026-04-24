"""
Public package surface for vommit.
"""

from . import tasks

__all__ = ["main", "tasks"]


def main() -> None:
    from .cli import program

    program.run()

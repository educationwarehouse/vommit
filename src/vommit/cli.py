import typing

try:
    import ewok
except ImportError:
    class _FallbackApp:
        def __init__(self, *_: typing.Any, **__: typing.Any) -> None:
            pass

        def run(self, *_: typing.Any, **__: typing.Any) -> None:
            raise RuntimeError("ewok is required to run the vommit CLI")

    class _FallbackEwokModule:
        App = _FallbackApp

    ewok = _FallbackEwokModule()

from . import tasks
from .__about__ import __version__

program = ewok.App(
    "vommit",
    version=__version__,
    core_module=tasks,
    plugin_entrypoint=("edwh", "vommit.tasks"),
)

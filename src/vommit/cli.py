import ewok

from . import tasks
from .__about__ import __version__

program = ewok.App(
    "vommit",
    version=__version__,
    core_module=tasks,
    plugin_entrypoint=("vommit", "vommit.tasks"),
)

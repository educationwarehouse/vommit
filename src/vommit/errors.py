class VommitError(Exception):
    """
    Raised when vommit cannot continue for a reason the user should see.

    The library layer stays free of `invoke`; entrypoints in `tasks.py`
    translate this into an `invoke.Exit`.
    """

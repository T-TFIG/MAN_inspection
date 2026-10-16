"""MAN_inspection activity logger package.

Keep this module lightweight: it is imported in CI just to read the version,
so it must not pull in Windows-only dependencies at import time.
"""

__version__ = "0.4.21"

"""SLOP backend package.

`__version__` is the single source of truth for the running product version.
It is surfaced by the API (`/api/ping` payload and the FastAPI/OpenAPI docs
header). Keep `installer/main.py::_INSTALLER_VERSION` in sync when releasing.
"""

__version__ = "5.1.0"

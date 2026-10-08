"""The application package.

`__version__` lives here rather than in `main` so that anything can read it.
It was in `main.py`, which imports every router -- so a router wanting the
version had to either import `main` (a cycle) or be handed a shim module. One
line in the package root costs neither.
"""

from __future__ import annotations

__version__ = "0.9.1"

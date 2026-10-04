"""One-time Import: another budgeting app's history, brought in once (#183).

Two halves, and the seam between them is the point:

- a **workflow** per app reads that app's export or API into a
  :class:`~.model.Source` -- ``ynab_source`` is the first, with ``ynab_api``
  as its only network code;
- the **engine** (``engine``) does everything after that, the same way for
  every app: suggestions, the account and category mapping, duplicates,
  transfers, and one batch that one undo reverses.

Another app is another reader registered in :data:`WORKFLOWS`; nothing in the
engine should need to know which app a row came from.
"""

from __future__ import annotations

from . import engine, ynab_source
from .model import Source, SourceAccount, SourceRow

#: app -> the module that reads it. Keyed by the name in the route.
WORKFLOWS = {"ynab": ynab_source}

__all__ = ["WORKFLOWS", "Source", "SourceAccount", "SourceRow", "engine", "ynab_source"]

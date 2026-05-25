"""Unified cache layer (DES-9).

A small, dependency-free backend with a minimal interface (``get`` /
``set``):

- :class:`cache.disk.DiskCache` — atomic-write JSON file persistence.
  Used by :mod:`fooby_cache` to survive container restarts.

Import the backend directly: ``from cache.disk import DiskCache``.
"""

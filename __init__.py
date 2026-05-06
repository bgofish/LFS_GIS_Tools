"""Viewport Export Plugin for LichtFeld Studio.

Export the current viewport as a JPG image at 1080p, 4K, or 8K resolution.
"""

import lichtfeld as lf

from .panels.export_panel import ViewportExportPanel

_classes = [ViewportExportPanel]


def on_load():
    """Called when plugin loads."""
    for cls in _classes:
        lf.register_class(cls)
    lf.log.info("Viewport Export plugin loaded")


def on_unload():
    """Called when plugin unloads."""
    for cls in reversed(_classes):
        lf.unregister_class(cls)
    lf.log.info("Viewport Export plugin unloaded")

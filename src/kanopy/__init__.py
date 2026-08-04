"""Official Python client for the Kanopy Developer API."""

from .client import DEFAULT_BASE_URL, Kanopy
from .errors import KanopyError, KanopyUploadError
from .models import Page

__all__ = ["DEFAULT_BASE_URL", "Kanopy", "KanopyError", "KanopyUploadError", "Page"]
__version__ = "0.4.0"

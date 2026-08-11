"""Single source of truth for the package version.

Lives in its own module so client.py can build the User-Agent from it
without importing the package __init__ (which imports client.py back).
"""

__version__ = "0.5.0"

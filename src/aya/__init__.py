"""aya — personal AI assistant toolkit."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aya-ai-assist")
except PackageNotFoundError:
    __version__ = "0.0.0"

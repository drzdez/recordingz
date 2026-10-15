"""recordingz: post-processing (transcription, speakers, minutes) of meeting recordings."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("recordingz")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

APP_NAME = "recordingz"
FORMAT_VERSION = 1

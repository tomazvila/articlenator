"""Flask route blueprints."""

from .api import api_bp
from .channel import channel_bp
from .pages import pages_bp
from .transcription import transcription_bp

__all__ = ["api_bp", "channel_bp", "pages_bp", "transcription_bp"]

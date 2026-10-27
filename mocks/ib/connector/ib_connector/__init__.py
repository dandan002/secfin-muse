"""Desk Chat (IB-style) bot connector. See ../../API.md."""

from ._http import ApiError
from .client import Chip, DeskChatBot, Message, Stream, User

__all__ = ["ApiError", "Chip", "DeskChatBot", "Message", "Stream", "User"]

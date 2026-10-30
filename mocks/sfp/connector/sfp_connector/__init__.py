"""Processor (FIS-style) connector. See ../../API.md."""

from ._http import ApiError
from .cdm import securities_lending_trade
from .client import AmendResult, Booking, Break, CompareResult, Contract, Diff, MarginCall, ProcessorClient

__all__ = ["AmendResult", "ApiError", "Booking", "Break", "CompareResult", "Contract", "Diff", "MarginCall",
           "ProcessorClient", "securities_lending_trade"]

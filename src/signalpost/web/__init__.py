"""Website discovery, identity verification, crawl and extraction.

Public entry point: `signalpost.web.connector.WebConnector`.
"""
from __future__ import annotations

from .connector import WebConnector

__all__ = ["WebConnector"]

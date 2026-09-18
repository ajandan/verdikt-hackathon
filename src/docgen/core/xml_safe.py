"""Hardened lxml parser factory for user-supplied XML.

Every DOCX ingest, template diff, and verdict-scoring code path reads XML
that originates outside the trust boundary. A crafted document with an
external DTD reference, a billion-laughs entity chain, or an XInclude
directive would otherwise let an attacker DoS a worker or force it to
fetch attacker-chosen URLs. Callers wire ``safe_fromstring`` in place of
``etree.fromstring`` so the same three flags (``resolve_entities=False``,
``no_network=True``, ``load_dtd=False``) apply everywhere the parser is
handed untrusted input.
"""
from __future__ import annotations

from lxml import etree


def safe_xml_parser() -> etree.XMLParser:
    """Return an lxml parser that refuses external entities + DTD fetches."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
    )


def safe_fromstring(payload: bytes | str):
    """``etree.fromstring`` with the hardened parser from :func:`safe_xml_parser`."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return etree.fromstring(data, parser=safe_xml_parser())


__all__ = ["safe_fromstring", "safe_xml_parser"]

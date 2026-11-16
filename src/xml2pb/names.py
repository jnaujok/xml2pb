"""Name helpers shared by the converter and the .proto generator."""

from __future__ import annotations

import re


def norm(name: str) -> str:
    """Case/separator-insensitive key: ``order_id``, ``orderId``, ``Order-ID`` -> ``orderid``."""
    return re.sub(r"[_\-.]", "", name).lower()


def local(key: object) -> str:
    """Strip the ``@`` attribute marker and any namespace prefix or ``{uri}``.

    >>> local("@{urn:x}ns:id")
    'id'
    """
    k = str(key).lstrip("@")
    return k.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def camel(s: str) -> str:
    """``order_line`` / ``order-line`` / ``orderLine`` -> ``OrderLine`` (message/enum names)."""
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", s) if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts) or "Message"
    return out if out[0].isalpha() else "M" + out


def snake(s: str) -> str:
    """``orderID`` / ``OrderLine`` / ``order-line`` -> ``order_id`` / ``order_line`` (fields)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower() or "field"
    return "f_" + s if s[0].isdigit() else s

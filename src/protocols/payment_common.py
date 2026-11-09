"""src/protocols/payment_common.py  Small helpers shared by the three payment protocols."""
from typing import Type

from spade.message import Message

from common.ontology import parse_payload


def bare(jid) -> str:
    """'user@domain/resource' -> 'user@domain'."""
    return str(jid).split("/")[0]


def refusal_body(reason: str, detail: str = "") -> str:
    """Body of a `refuse`: '<reason>' or '<reason>:<detail>' (reason is a ledger.Reason value)."""
    return f"{reason}:{detail}" if detail else reason


def expect(msg: Message, cls: Type):
    """Payload of `msg` as `cls`, strictly parsed. ValueError if absent or of another type
    (the FSM turns that into a protocol-level failure reply)."""
    payload = parse_payload(msg, strict=True)
    if not isinstance(payload, cls):
        got = type(payload).__name__ if payload is not None else "no payload"
        raise ValueError(f"expected {cls.__name__}, got {got}")
    return payload

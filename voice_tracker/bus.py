from __future__ import annotations

import base64
import hmac
import inspect
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from .domain import (
    SUBJECT_ACTIVITY_EVENT,
    SUBJECT_SESSION_CLOSED,
    SUBJECT_SUMMARY_READY,
    SUBJECT_VOICE_EVENT,
    to_jsonable,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(to_jsonable(value), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _as_bytes(value: bytes | bytearray | str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode("utf-8")


class Deduper(Protocol):
    def claim_message(
        self,
        ctx: Any,
        subject: str,
        message_id: str,
        issuer: str,
        issued_at: int,
    ) -> bool | Awaitable[bool]:
        ...


@dataclass(slots=True)
class Envelope:
    message_id: str
    subject: str
    issuer: str
    issued_at: int
    payload: bytes
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "subject": self.subject,
            "issuer": self.issuer,
            "issuedAt": self.issued_at,
            "payload": json.loads(self.payload.decode("utf-8")),
            "signature": self.signature,
        }


class Bus:
    def __init__(self, conn: Any, secret: bytes | str, issuer: str) -> None:
        self.conn = conn
        self.secret = _as_bytes(secret)
        self.issuer = issuer.strip()
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    async def connect(
        cls,
        url: str,
        secret: bytes | str,
        issuer: str,
        connector: Callable[[str], Awaitable[Any]],
    ) -> "Bus":
        if not str(secret).strip():
            raise ValueError("event signing secret is required")
        conn = await connector(url)
        return cls(conn, secret, issuer)

    def close(self) -> Any:
        if self.conn is None:
            return
        close = getattr(self.conn, "close", None)
        if callable(close):
            return close()
        return None

    async def aclose(self) -> None:
        result = self.close()
        if inspect.isawaitable(result):
            await result

    async def publish_json(self, *args: Any) -> None:
        if len(args) == 2:
            subject, value = args
        elif len(args) == 3:
            _, subject, value = args
        else:
            raise TypeError("publish_json expects subject/value or ctx/subject/value")
        if self.conn is None:
            raise ValueError("nats connection is nil")
        payload = _json_bytes(value)
        env = Envelope(
            message_id=str(uuid4()),
            subject=subject,
            issuer=self.issuer,
            issued_at=int(_utc_now().timestamp()),
            payload=payload,
            signature="",
        )
        env.signature = sign_envelope(self.secret, env.message_id, env.subject, env.issuer, env.issued_at, env.payload)
        body = json.dumps(env.to_dict(), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        publish = getattr(self.conn, "publish", None)
        if not callable(publish):
            raise TypeError("connection does not support publish")
        result = publish(subject, body)
        if inspect.isawaitable(result):
            await result

    PublishJSON = publish_json

    async def subscribe(
        self,
        ctx: Any,
        subject: str,
        deduper: Deduper | None,
        handler: Callable[[bytes], Any],
    ) -> Any:
        if self.conn is None:
            raise ValueError("nats connection is nil")

        async def _callback(msg: Any) -> None:
            data = getattr(msg, "data", msg)
            try:
                env, payload = decode_envelope(self.secret, subject, data)
            except Exception as exc:
                logger.warning("nats envelope error subject=%s: %s", subject, exc)
                return

            if deduper is not None:
                try:
                    claimed = deduper.claim_message(ctx, subject, env.message_id, env.issuer, env.issued_at)
                    if inspect.isawaitable(claimed):
                        claimed = await claimed
                except Exception:
                    logger.exception("nats claim error subject=%s id=%s", subject, env.message_id)
                    return
                if not claimed:
                    logger.info("nats duplicate dropped subject=%s id=%s", subject, env.message_id)
                    return
            elif self._seen_message(env.message_id, env.issued_at):
                logger.info("nats duplicate dropped subject=%s id=%s", subject, env.message_id)
                return

            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("nats handler error subject=%s id=%s", subject, env.message_id)

        subscribe = getattr(self.conn, "subscribe", None)
        if not callable(subscribe):
            raise TypeError("connection does not support subscribe")
        result = subscribe(subject, cb=_callback)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _seen_message(self, message_id: str, issued_at: int) -> bool:
        cutoff = int((_utc_now() - timedelta(hours=2)).timestamp())
        with self._lock:
            stale = [key for key, ts in self._seen.items() if ts < cutoff]
            for key in stale:
                self._seen.pop(key, None)
            if message_id in self._seen:
                return True
            self._seen[message_id] = issued_at
            return False


def sign_envelope(
    secret: bytes | str,
    message_id: str,
    subject: str,
    issuer: str,
    issued_at: int,
    payload: bytes | bytearray | str,
) -> str:
    mac = hmac.new(_as_bytes(secret), digestmod="sha256")
    mac.update(message_id.encode("utf-8"))
    mac.update(b"|")
    mac.update(subject.encode("utf-8"))
    mac.update(b"|")
    mac.update(issuer.encode("utf-8"))
    mac.update(b"|")
    mac.update(str(int(issued_at)).encode("utf-8"))
    mac.update(b"|")
    mac.update(_as_bytes(payload))
    return base64.b64encode(mac.digest()).decode("ascii")


def decode_envelope(secret: bytes | str, expected_subject: str, data: bytes | bytearray | str) -> tuple[Envelope, bytes]:
    raw = _as_bytes(data)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid envelope")

    env = Envelope(
        message_id=str(payload.get("messageId", "")),
        subject=str(payload.get("subject", "")),
        issuer=str(payload.get("issuer", "")),
        issued_at=int(payload.get("issuedAt") or 0),
        payload=_json_bytes(payload.get("payload")),
        signature=str(payload.get("signature", "")),
    )

    if env.subject != expected_subject:
        raise ValueError(f'unexpected subject "{env.subject}"')

    expected_issuer = issuer_for_subject(expected_subject)
    if expected_issuer and env.issuer != expected_issuer:
        raise ValueError(f'unexpected issuer "{env.issuer}"')

    if env.message_id == "":
        raise ValueError("missing messageId")
    if env.issued_at == 0:
        raise ValueError("missing issuedAt")

    age = _utc_now() - datetime.fromtimestamp(env.issued_at, UTC)
    if age < -timedelta(minutes=5) or age > timedelta(hours=1):
        raise ValueError("stale envelope")

    expected_signature = sign_envelope(secret, env.message_id, env.subject, env.issuer, env.issued_at, env.payload)
    if not hmac.compare_digest(expected_signature, env.signature):
        raise ValueError("invalid signature")

    return env, env.payload


def issuer_for_subject(subject: str) -> str:
    if subject == SUBJECT_VOICE_EVENT:
        return "gateway"
    if subject == SUBJECT_ACTIVITY_EVENT:
        return "gateway"
    if subject == SUBJECT_SESSION_CLOSED:
        return "tracker"
    if subject == SUBJECT_SUMMARY_READY:
        return "writer"
    return ""

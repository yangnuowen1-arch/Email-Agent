"""Application-owned ports for replaceable external capabilities."""

from app.ports.inbound_mail import InboundMailbox
from app.ports.sync_store import EmailSyncStore

__all__ = ["EmailSyncStore", "InboundMailbox"]

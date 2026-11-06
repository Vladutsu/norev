"""Composition root: the only module that builds the real LLM client (ADR 0001, #22).

UI routes and use cases get the CentralProcessor, never a network client or a real Connector;
tests/test_processor_composition.py fails if another module under norev/ imports one.
"""
from collections.abc import Iterable
from datetime import datetime

from LLM.AI_Model.client import build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings
from norev.core.ports import Clock, Connector, Diarizer, Transcriber
from norev.processor.central import CentralProcessor, ExecutionAuditLog
from norev.processor.identity import LocalUser


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


def build_central_processor(
    audit: ExecutionAuditLog,
    *,
    connectors: Iterable[Connector] = (),
    transcriber: Transcriber | None = None,
    diarizer: Diarizer | None = None,
    settings: DeepSeekSettings | None = None,
    identity: LocalUser | None = None,
    clock: Clock | None = None,
) -> CentralProcessor:
    """The real processor. The LLM settings come from the environment (README.md); building it does
    not connect to anything. There is no default audit log: without one nothing can be sent."""
    return CentralProcessor(
        build_llm_client(settings or DeepSeekSettings.from_environment()),
        audit,
        identity=identity or LocalUser.from_os(),
        clock=clock or SystemClock(),
        connectors=connectors,
        transcriber=transcriber,
        diarizer=diarizer,
    )

"""Composition root: the only module that builds the real LLM client (ADR 0001, #22).

UI routes and use cases get the CentralProcessor, never a network client or a real Connector;
tests/test_processor_composition.py fails if another module under norev/ imports one.
"""
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from LLM.AI_Model.client import build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings
from norev.adapters.connectors.local_document import LocalDocumentConnector
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


# The app's local Workflow steps (#23), by the names Domains use. The document is built at preview
# time (its exact text is the approved payload) and the executor saves each step and the action's
# status itself, so neither step has more to do.
LOCAL_STEPS = {"build_document": lambda action: None, "save_result": lambda action: None}


def build_desktop_processor(
    store: ExecutionAuditLog, export_dir: Path, *, transcriber: Transcriber | None = None, diarizer: Diarizer | None = None,
) -> CentralProcessor:
    """The desktop app's processor (#43): the local_document Connector writes into `export_dir`."""
    return build_central_processor(store, connectors=[LocalDocumentConnector(export_dir)],
                                   transcriber=transcriber, diarizer=diarizer)

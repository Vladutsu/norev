"""Composition root: the only module that builds the real LLM client (ADR 0001, #22).

UI routes and use cases get the CentralProcessor, never a network client or a real Connector;
tests/test_processor_composition.py fails if another module under norev/ imports one.
"""
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from LLM.AI_Model.client import build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings
from norev.adapters.connectors.google_calendar import (
    GoogleAuth,
    GoogleCalendarConnector,
    GoogleCalendarSettings,
    SecretStore,
)
from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.core.ports import Clock, Connector, Diarizer, PayloadBuilder, Transcriber
from norev.processor.central import CentralProcessor, ExecutionAuditLog
from norev.processor.google_account import GoogleAccount
from norev.processor.identity import LocalUser
from norev.processor.payloads import ConnectorPayloadBuilder, GoogleCalendarPayloadBuilder, LocalDocumentPayloadBuilder


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


# The app's local Workflow steps (#23), by the names Domains use. The document and the event are
# built at preview time (their exact content is the approved payload) and the executor saves each
# step and the action's status itself, so none of these steps has more to do.
LOCAL_STEPS = {"build_document": lambda action: None, "build_event": lambda action: None,
               "save_result": lambda action: None}


def build_desktop_processor(
    store: ExecutionAuditLog, export_dir: Path, *, transcriber: Transcriber | None = None, diarizer: Diarizer | None = None,
    calendar: GoogleCalendarConnector | None = None,
) -> CentralProcessor:
    """The desktop app's processor (#43): the local_document Connector writes into `export_dir`;
    `calendar` is the Google Calendar Connector when it is set up (build_google_calendar, #45)."""
    connectors = [LocalDocumentConnector(export_dir)] + ([calendar] if calendar is not None else [])
    return build_central_processor(store, connectors=connectors, transcriber=transcriber, diarizer=diarizer)


def desktop_payload_builder(export_dir: Path, *, calendar: bool) -> PayloadBuilder:
    """The previews of the desktop app (#18): the builder of each Workflow's Connector. Without Google
    Calendar set up, a calendar Action Type cannot be previewed (UnsupportedWorkflow), so nothing is
    approved that could not be sent."""
    builders: dict[str, PayloadBuilder] = {"local_document": LocalDocumentPayloadBuilder(export_dir)}
    if calendar:
        builders["google_calendar"] = GoogleCalendarPayloadBuilder.from_environment()
    return ConnectorPayloadBuilder(builders)


def build_google_calendar(
    store: object, settings: GoogleCalendarSettings | None = None,
) -> tuple[GoogleAccount, GoogleCalendarConnector | None]:
    """The User's Google account (connect/disconnect, for the UI) and the Google Calendar Connector
    (#45), which the app gives to build_desktop_processor. Without an OAuth client in the environment,
    or without a store that keeps secrets encrypted, there is no Connector and the account is
    unavailable. Building them connects to nothing."""
    settings = settings or GoogleCalendarSettings.from_environment()
    if not (settings.client_id and settings.client_secret) or not isinstance(store, SecretStore):
        return GoogleAccount(None), None
    auth = GoogleAuth(settings, store)
    return GoogleAccount(auth), GoogleCalendarConnector(auth)

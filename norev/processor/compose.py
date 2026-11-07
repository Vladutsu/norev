"""Composition root: the only module that builds the real LLM client (ADR 0001, #22).

UI routes and use cases get the CentralProcessor, never a network client or a real Connector;
tests/test_processor_composition.py fails if another module under norev/ imports one.
"""
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from LLM.AI_Model.client import build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings
from norev.adapters.connectors.email_draft import EmailDraftConnector
from norev.adapters.connectors.google_calendar import (
    GoogleAuth,
    GoogleCalendarConnector,
    GoogleCalendarSettings,
    SecretStore,
)
from norev.adapters.connectors.jira import JiraAuth, JiraConnector, JiraSettings
from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.connectors.trello import TrelloAccount, TrelloConnector, TrelloSettings
from norev.core.ports import Clock, Connector, Diarizer, PayloadBuilder, Transcriber
from norev.processor.central import CentralProcessor, ExecutionAuditLog
from norev.processor.google_account import GoogleAccount
from norev.processor.identity import LocalUser
from norev.processor.jira_account import JiraAccount
from norev.processor.payloads import (
    ConnectorPayloadBuilder,
    EmailDraftPayloadBuilder,
    GoogleCalendarPayloadBuilder,
    JiraPayloadBuilder,
    LocalDocumentPayloadBuilder,
    TrelloPayloadBuilder,
)
from norev.processor.trello_account import TrelloLink


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
    models=None,
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
        models=models,
    )


# The app's local Workflow steps (#23), by the names Domains use. The document, the event and the
# email draft are built at preview time (their exact content is the approved payload) and the executor saves each
# step and the action's status itself, so none of these steps has more to do.
LOCAL_STEPS = {"build_document": lambda action: None, "build_event": lambda action: None,
               "build_email": lambda action: None, "save_result": lambda action: None}


def build_desktop_processor(
    store: ExecutionAuditLog, export_dir: Path, *, transcriber: Transcriber | None = None, diarizer: Diarizer | None = None,
    calendar: GoogleCalendarConnector | None = None, trello: TrelloConnector | None = None,
    jira: JiraConnector | None = None, models=None,
) -> CentralProcessor:
    """The desktop app's processor (#43): the local Connectors (document, email draft #61) write into
    `export_dir`; `calendar`, `trello` and `jira` are the Google Calendar (#45), Trello (#72) and Jira
    (#73) Connectors when they are set up (build_google_calendar, build_trello, build_jira)."""
    connectors = [LocalDocumentConnector(export_dir), EmailDraftConnector(export_dir)]
    connectors += [c for c in (calendar, trello, jira) if c is not None]
    return build_central_processor(store, connectors=connectors, transcriber=transcriber, diarizer=diarizer, models=models)


def desktop_payload_builder(export_dir: Path, *, calendar: bool, trello: TrelloLink | None = None,
                            jira: JiraAccount | None = None,
                            member_of: Callable[[str], Any] = lambda email: None) -> PayloadBuilder:
    """The previews of the desktop app (#18): the builder of each Workflow's Connector, or of the
    destination the User chose (#72). Without Google Calendar or Trello set up, nothing can be
    previewed for them (UnsupportedWorkflow), so nothing is approved that could not be sent. The same
    holds for Jira (#73)."""
    builders: dict[str, PayloadBuilder] = {"local_document": LocalDocumentPayloadBuilder(export_dir),
                                           "email_draft": EmailDraftPayloadBuilder(export_dir)}
    if calendar:
        builders["google_calendar"] = GoogleCalendarPayloadBuilder.from_environment()
    if trello is not None and trello.status().get("key_settable"):  # usable once it is connected with a list
        builders["trello"] = TrelloPayloadBuilder.from_environment(trello.chosen_list, member_of)
    if jira is not None and jira.status()["available"]:
        builders["jira"] = JiraPayloadBuilder(jira.destination, member_of=member_of)
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


def build_trello(store: object, settings: TrelloSettings | None = None) -> tuple[TrelloLink, TrelloConnector | None]:
    """The User's Trello account (token, list, for the UI) and the Trello Connector (#72), which the app
    gives to build_desktop_processor. Without a store that keeps secrets encrypted there is none. The
    app's key comes from NOREV_TRELLO_API_KEY or is entered in the app by the Team Leader (ADR 0006);
    until then the account says it is not available. Connects to nothing."""
    settings = settings or TrelloSettings.from_environment()
    if not isinstance(store, SecretStore):  # the key may also come later, entered in the app (ADR 0006)
        return TrelloLink(None), None
    account = TrelloAccount(settings, store)
    return TrelloLink(account), TrelloConnector(account)


def build_jira(store: object, settings: JiraSettings | None = None) -> tuple[JiraAccount, JiraConnector | None]:
    """The User's Atlassian account and chosen project (for the UI) and the Jira Connector (#73), which
    the app gives to build_desktop_processor. Without an OAuth app in the environment, or without a
    store that keeps secrets encrypted, there is no Connector and the account is unavailable.
    Building them connects to nothing."""
    settings = settings or JiraSettings.from_environment()
    if not (settings.client_id and settings.client_secret) or not isinstance(store, SecretStore):
        return JiraAccount(None), None
    auth = JiraAuth(settings, store)
    return JiraAccount(auth), JiraConnector(auth)

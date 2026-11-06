"""Jira Cloud Connector (#73) against a fake Atlassian on 127.0.0.1: OAuth, project choice, send,
idempotency, errors, and a work item approved and executed through the Central Processor. No network."""
import json
import socket
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from norev.adapters.connectors.jira import (
    DESTINATION,
    REFRESH_TOKEN,
    SCOPES,
    AuthorizationFailed,
    DestinationChanged,
    JiraAuth,
    JiraConnector,
    JiraEndpoints,
    JiraError,
    JiraSettings,
    JiraUnreachable,
    NotConnected,
    ReconnectNeeded,
)
from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.core.models import ActionStatus, ProposedAction, Session, UnsupportedWorkflow, ValidationError
from norev.processor.central import CentralProcessor, SendRefused
from norev.processor.compose import DESTINATIONS, build_jira, desktop_payload_builder
from norev.processor.identity import LocalUser
from norev.processor.jira_account import JiraAccount
from norev.processor.payloads import JiraPayloadBuilder
from norev.processor.workflow import WorkflowExecutor
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.usecases.approve import approve_action, preview_action
from test_connectors_google_auth import MemorySecrets
from test_processor_send import RecordingAuditLog
from test_processor_workflow import InMemoryExecutionStore

AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
SECRET = "Ion Popescu, CNP 1800101123456"
CORPORATE = load_domain(DOMAINS / "corporate")
MEDICAL = load_domain(DOMAINS / "medical")
CLOUD = "11223344-a1b2-3b33-c444-def123456789"
SITE = {"cloud_id": CLOUD, "url": "https://acme-test.atlassian.net", "name": "acme-test"}
CHOSEN = {**SITE, "project_key": "OPS", "project_name": "Operations"}
LABEL = "norev-" + "a" * 32
GRANTED = " ".join(SCOPES)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAtlassian:
    """Token, accessible-resources and the Jira REST calls the Connector makes."""

    def __init__(self, timeline: list[str] | None = None) -> None:
        self.timeline = timeline if timeline is not None else []
        self.requests: list[tuple[str, str, dict | None, str]] = []  # method, path, body, token
        self.issues: list[dict] = []
        self.fail_next: list[tuple[int, dict]] = []
        self.token_answers: list[tuple[int, dict]] = []
        self.lose_answer = False
        self.valid_tokens = {"access-1", "access-2"}
        self.issued = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                fake.requests.append((method, self.path, body, token))
                status, answer = fake.answer(method, self.path, body, token)
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                self._handle("POST")

            def do_GET(self) -> None:
                self._handle("GET")

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.endpoints = JiraEndpoints(auth=base + "/authorize", token=base + "/oauth/token",
                                       resources=base + "/resources", api=base + "/ex/jira")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, method: str, path: str, body: dict | None, token: str) -> tuple[int, object]:
        parts = urllib.parse.urlsplit(path)
        if parts.path == "/oauth/token":
            if self.token_answers:
                return self.token_answers.pop(0)
            self.issued += 1
            return 200, {"access_token": f"access-{self.issued}", "expires_in": 3600, "scope": GRANTED,
                         "refresh_token": f"refresh-{self.issued + 1}"}
        if token not in self.valid_tokens:
            return 401, {"code": 401, "message": "Unauthorized"}
        if self.fail_next:
            return self.fail_next.pop(0)
        if parts.path == "/resources":
            return 200, [{**{"id": CLOUD, "url": SITE["url"], "name": SITE["name"]}, "scopes": list(SCOPES[:2])},
                         {"id": "read-only-site", "url": "https://other.atlassian.net", "name": "other", "scopes": ["read:jira-work"]}]
        if parts.path.endswith("/rest/api/3/project/search"):
            return 200, {"values": [{"id": "1", "key": "OPS", "name": "Operations"}, {"id": "2", "key": "bad key", "name": "x"}]}
        self.timeline.append(method)
        if parts.path == f"/ex/jira/{CLOUD}/rest/api/3/search/jql":
            jql = urllib.parse.parse_qs(parts.query)["jql"][0]
            found = [i for i in self.issues if f'labels = "{i["fields"]["labels"][0]}"' in jql]
            return 200, {"issues": [{"id": i["id"], "key": i["key"]} for i in found[:1]]}
        if parts.path == f"/ex/jira/{CLOUD}/rest/api/2/issue" and method == "POST":
            issue = {"id": str(10000 + len(self.issues)), "key": f"OPS-{len(self.issues) + 1}", "fields": body["fields"]}
            self.issues.append(issue)
            if self.lose_answer:
                self.lose_answer = False
                return 503, {"errorMessages": ["Service unavailable"]}
            return 201, {"id": issue["id"], "key": issue["key"], "self": "https://x/rest/api/2/issue/" + issue["id"]}
        return 404, {"errorMessages": ["not found"]}


def payload(**fields) -> dict:
    issue_fields = {"project": {"key": "OPS"}, "issuetype": {"name": "Task"}, "summary": "Send the offer",
                    "description": f"Prepare the offer for {SECRET}.", "labels": [LABEL], **fields}
    return {"site": dict(SITE), "project": {"key": "OPS", "name": "Operations"}, "issue": {"fields": issue_fields}}


class JiraAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.atlassian = FakeAtlassian()
        self.addCleanup(self.atlassian.close)
        self.secrets = MemorySecrets()
        self.settings = JiraSettings("client-1", "secret-1", free_port())

    def auth(self, open_browser=None) -> JiraAuth:
        return JiraAuth(self.settings, self.secrets, endpoints=self.atlassian.endpoints,
                        open_browser=open_browser or self.browser(), timeout=5)

    def browser(self, **answer):
        """The User's browser: signs in and comes back to the redirect with a code and our state."""
        self.opened: list[dict] = []

        def open_browser(url: str) -> bool:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
            self.opened.append(query)
            back = {"code": "code-1", "state": query["state"], **answer}
            threading.Thread(target=lambda: urllib.request.urlopen(
                query["redirect_uri"] + "/?" + urllib.parse.urlencode(back), timeout=5).read(), daemon=True).start()
            return True
        return open_browser

    def test_connect_keeps_the_refresh_token_with_minimal_scopes(self) -> None:
        self.atlassian.token_answers = [(200, {"access_token": "access-1", "expires_in": 3600, "scope": GRANTED,
                                               "refresh_token": "refresh-1"})]
        auth = self.auth()
        auth.connect(wait=5)
        [query] = self.opened
        self.assertEqual(query["audience"], "api.atlassian.com")
        self.assertEqual(query["scope"].split(), list(SCOPES))
        self.assertEqual(query["redirect_uri"], f"http://127.0.0.1:{self.settings.redirect_port}")
        self.assertEqual(query["prompt"], "consent")
        exchange = next(body for _, path, body, _ in self.atlassian.requests if path == "/oauth/token")
        self.assertEqual(exchange, {"client_id": "client-1", "client_secret": "secret-1", "grant_type": "authorization_code",
                                    "code": "code-1", "redirect_uri": query["redirect_uri"]})
        self.assertEqual(self.secrets.values, {REFRESH_TOKEN: "refresh-1"})
        self.assertEqual(auth.access_token(), "access-1")  # in memory, no second exchange

    def test_refused_sign_in_saves_nothing(self) -> None:
        auth = self.auth(self.browser(error="access_denied", code=""))
        with self.assertRaisesRegex(AuthorizationFailed, "access_denied|no_code"):
            auth.connect(wait=5)
        self.assertEqual(self.secrets.values, {})

    def test_missing_scope_saves_nothing(self) -> None:
        self.atlassian.token_answers = [(200, {"access_token": "a", "expires_in": 3600, "scope": "read:jira-work",
                                               "refresh_token": "r"})]
        with self.assertRaisesRegex(AuthorizationFailed, "scope_not_granted"):
            self.auth().connect(wait=5)
        self.assertEqual(self.secrets.values, {})

    def test_port_in_use_is_a_clear_failure(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.settings.redirect_port))
            busy.listen()
            with self.assertRaisesRegex(AuthorizationFailed, "port_in_use"):
                self.auth().connect(wait=1)

    def test_refresh_rotates_the_refresh_token(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-old"
        self.atlassian.token_answers = [(200, {"access_token": "access-1", "expires_in": 3600, "scope": GRANTED,
                                               "refresh_token": "refresh-new"})]
        self.assertEqual(self.auth().access_token(), "access-1")
        [(_, _, body, _)] = self.atlassian.requests
        self.assertEqual(body, {"client_id": "client-1", "client_secret": "secret-1",
                                "grant_type": "refresh_token", "refresh_token": "refresh-old"})
        self.assertEqual(self.secrets.values[REFRESH_TOKEN], "refresh-new")

    def test_refused_refresh_token_needs_a_new_connection(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-old"
        self.atlassian.token_answers = [(403, {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})]
        with self.assertRaises(ReconnectNeeded):
            self.auth().access_token()
        self.assertNotIn(REFRESH_TOKEN, self.secrets.values)

    def test_disconnect_deletes_token_and_destination(self) -> None:
        self.secrets.values.update({REFRESH_TOKEN: "r", DESTINATION: json.dumps(CHOSEN)})
        self.auth().disconnect()
        self.assertEqual(self.secrets.values, {})
        self.assertEqual(self.atlassian.requests, [])  # no revocation API: nothing to call

    def test_not_connected(self) -> None:
        with self.assertRaises(NotConnected):
            self.auth().access_token()

    def test_settings_from_environment(self) -> None:
        settings = JiraSettings.from_environment({"NOREV_JIRA_CLIENT_ID": "i", "NOREV_JIRA_CLIENT_SECRET": "s"})
        self.assertEqual((settings.client_id, settings.redirect_uri), ("i", "http://127.0.0.1:8765"))
        self.assertNotIn("client_secret", repr(settings))
        with self.assertRaises(ValueError):
            JiraSettings.from_environment({"NOREV_JIRA_REDIRECT_PORT": "80"})


class JiraAccountTest(unittest.TestCase):
    """What the UI gets: the sites and projects to choose from, and the choice, kept encrypted."""

    def setUp(self) -> None:
        self.atlassian = FakeAtlassian()
        self.addCleanup(self.atlassian.close)
        self.secrets = MemorySecrets(**{REFRESH_TOKEN: "refresh-1"})
        auth = JiraAuth(JiraSettings("c", "s"), self.secrets, endpoints=self.atlassian.endpoints, timeout=5)
        self.account = JiraAccount(auth)

    def test_lists_only_writable_sites_and_valid_projects(self) -> None:
        listed = self.account.projects()
        self.assertIsNone(listed["failed"])
        self.assertEqual(listed["sites"], [{**SITE, "projects": [{"key": "OPS", "name": "Operations"}]}])

    def test_choose_keeps_the_destination_only_if_accessible(self) -> None:
        self.assertEqual(self.account.choose(CLOUD, "NOPE")["failed"], "not_accessible")
        self.assertIsNone(self.account.destination())
        chosen = self.account.choose(CLOUD, "OPS")
        self.assertEqual((chosen["failed"], chosen["destination"]), (None, CHOSEN))
        self.assertEqual(json.loads(self.secrets.values[DESTINATION]), CHOSEN)
        self.assertIsNone(self.account.choose(None, None)["destination"])  # back to local documents

    def test_unavailable_account(self) -> None:
        account = JiraAccount(None)
        self.assertEqual(account.status(), {"available": False, "connected": False, "destination": None})
        self.assertEqual(account.connect()["failed"], "not_configured")


class StubAuth:
    def __init__(self, store: MemorySecrets, endpoints: JiraEndpoints) -> None:
        self.store = store
        self.endpoints = endpoints
        self.tokens = ["access-1"]
        self.forgotten = 0

    def access_token(self) -> str:
        if not self.tokens:
            raise NotConnected("no Atlassian account connected")
        return self.tokens[0]

    def forget_access_token(self) -> None:
        self.forgotten += 1
        self.tokens.pop(0)


class JiraConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.atlassian = FakeAtlassian()
        self.addCleanup(self.atlassian.close)
        self.secrets = MemorySecrets(**{DESTINATION: json.dumps(CHOSEN)})
        self.auth = StubAuth(self.secrets, self.atlassian.endpoints)
        self.connector = JiraConnector(self.auth, timeout=5)

    def test_searches_by_label_then_creates_exactly_the_issue(self) -> None:
        result = self.connector.send(payload(duedate="2026-11-20"))
        self.assertEqual(result, {"issue_key": "OPS-1", "created": True, "link": "https://acme-test.atlassian.net/browse/OPS-1"})
        search, create = self.atlassian.requests
        self.assertIn(f'labels = "{LABEL}"', urllib.parse.unquote_plus(search[1]))
        self.assertIn('project = "OPS"', urllib.parse.unquote_plus(search[1]))
        self.assertEqual(create[2], payload(duedate="2026-11-20")["issue"])
        self.assertNotIn("assignee", create[2]["fields"])

    def test_sending_again_finds_the_issue_instead_of_a_second_one(self) -> None:
        self.connector.send(payload())
        again = self.connector.send(payload())
        self.assertEqual((again["issue_key"], again["created"]), ("OPS-1", False))
        self.assertEqual(len(self.atlassian.issues), 1)

    def test_another_project_than_the_chosen_one_is_refused_without_a_call(self) -> None:
        other = payload()
        other["project"] = {"key": "HR", "name": "HR"}
        other["issue"]["fields"]["project"] = {"key": "HR"}
        with self.assertRaises(DestinationChanged):
            self.connector.send(other)
        self.secrets.values.pop(DESTINATION)
        with self.assertRaises(DestinationChanged):
            self.connector.send(payload())
        self.assertEqual(self.atlassian.requests, [])

    def test_payloads_not_shaped_like_the_builders_are_refused_without_a_call(self) -> None:
        bad = [
            {**payload(), "extra": 1},
            payload(assignee={"id": "x"}),
            payload(labels=["other"]),
            payload(labels=[LABEL, "norev-" + "b" * 32]),
            payload(project={"key": "HR"}),
            {**payload(), "site": {**SITE, "cloud_id": "../../admin"}},
            {**payload(), "site": {**SITE, "url": "http://acme.atlassian.net"}},
        ]
        for wrong in bad:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.connector.send(wrong)
        self.assertEqual(self.atlassian.requests, [])

    def test_expired_access_token_is_refreshed_once(self) -> None:
        self.auth.tokens = ["expired", "access-2"]
        self.assertTrue(self.connector.send(payload())["created"])
        self.assertEqual(self.auth.forgotten, 1)

    def test_errors_keep_only_the_status_and_field_names(self) -> None:
        cases = [
            (400, {"errorMessages": [], "errors": {"customfield_10020": f"Sprint is required for {SECRET}"}}, "fields:customfield_10020"),
            (429, {"errorMessages": [f"Rate limit for {SECRET}"]}, None),
            (500, {"errorMessages": [SECRET]}, None),
        ]
        for status, body, code in cases:
            self.atlassian.fail_next = [(status, body)]
            with self.subTest(status=status), self.assertRaises(JiraError) as caught:
                self.connector.send(payload())
            self.assertEqual((caught.exception.status, caught.exception.error), (status, code))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_unreachable(self) -> None:
        self.atlassian.close()
        with self.assertRaises(JiraUnreachable):
            self.connector.send(payload())


class JiraWorkflowTest(unittest.TestCase):
    """A Corporate work item, previewed as a Jira issue because the User chose a project, approved
    through #18's use cases and executed by #23's executor through the Central Processor (#22)."""

    def setUp(self) -> None:
        self.timeline: list[str] = []
        self.atlassian = FakeAtlassian(self.timeline)
        self.addCleanup(self.atlassian.close)
        self.secrets = MemorySecrets(**{REFRESH_TOKEN: "refresh-1", DESTINATION: json.dumps(CHOSEN)})
        auth = JiraAuth(JiraSettings("c", "s"), self.secrets, endpoints=self.atlassian.endpoints, timeout=5)
        self.account = JiraAccount(auth)
        self.audit = RecordingAuditLog(self.timeline)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.export = Path(temp.name)
        self.processor = CentralProcessor(FakeLlmClient([]), self.audit, identity=LocalUser("local:processor"),
                                          clock=FixedClock(AT), connectors=[JiraConnector(auth, timeout=5),
                                                                            LocalDocumentConnector(self.export)])
        self.builder = desktop_payload_builder(self.export, calendar=False, jira=self.account)
        self.sessions = InMemorySessionStore()
        self.steps = InMemoryExecutionStore()
        self.sessions.save_session(Session(id="s1", started_at=AT, domain="corporate"))
        action = ProposedAction("s1-1", "Send the offer", f"Prepare the offer for {SECRET}.", "I'll send it by Friday",
                                owner="S1", due_date=date(2026, 11, 20), action_type="work_item",
                                fields={"deliverable": "The offer PDF"})
        revision = self.sessions.save_actions("s1", [action], expected_revision=0)
        snapshot = preview_action("s1", "s1-1", {}, expected_revision=revision, store=self.sessions, domain=CORPORATE,
                                  builder=self.builder, destinations=DESTINATIONS)
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=self.sessions, domain=CORPORATE,
                       clock=FixedClock(AT), approved_by="local:ana", destinations=DESTINATIONS)
        [self.step] = self.action().approved_payload["steps"]
        local = {"build_document": lambda a: None, "save_result": lambda a: None}
        self.executor = WorkflowExecutor(self.processor, self.sessions, self.steps, CORPORATE, local,
                                         destinations=DESTINATIONS)

    def action(self) -> ProposedAction:
        return self.sessions.load_actions("s1")[0]

    def test_the_preview_is_the_exact_issue_and_where_it_goes(self) -> None:
        self.assertEqual(self.step["connector"], "jira")
        p = self.step["payload"]
        self.assertEqual((p["site"], p["project"]), (SITE, {"key": "OPS", "name": "Operations"}))
        fields = p["issue"]["fields"]
        self.assertEqual(fields["summary"], "Send the offer")
        self.assertEqual(fields["duedate"], "2026-11-20")
        self.assertEqual(fields["issuetype"], {"name": "Task"})
        self.assertNotIn("assignee", fields)  # S1 is not a Jira user
        self.assertEqual(fields["description"].split("\n\n"), [
            f"Prepare the offer for {SECRET}.", "Responsabil: S1", "Livrabil: The offer PDF",
            "Citat din Transcript: I'll send it by Friday"])
        [label] = fields["labels"]
        self.assertRegex(label, r"^norev-[0-9a-f]{32}$")

    def test_the_approved_issue_is_audited_then_sent_exactly(self) -> None:
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "executed")
        self.assertEqual(self.timeline, ["audit", "GET", "POST", "ok"])
        self.assertEqual(self.atlassian.issues[0]["fields"], self.step["payload"]["issue"]["fields"])
        self.assertEqual(self.audit.events[0].connector, "jira")
        self.assertEqual(self.audit.events[0].payload, self.step["payload"])
        self.assertEqual(outcome.steps["save_document"].result["issue_key"], "OPS-1")

    def test_audit_failure_sends_nothing(self) -> None:
        self.audit.fail = True
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.atlassian.requests, [])

    def test_manual_retry_after_a_lost_answer_creates_one_issue(self) -> None:
        self.atlassian.lose_answer = True
        self.assertEqual(self.executor.execute("s1", "s1-1").status, "failed")
        self.assertEqual(self.action().error, "Step save_document (jira) failed (JiraError).")
        self.assertNotIn(SECRET, self.action().error)
        self.executor.retry("s1", "s1-1")
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "executed")
        self.assertFalse(outcome.steps["save_document"].result["created"])
        self.assertEqual(len(self.atlassian.issues), 1)

    def test_project_changed_after_approval_is_refused_without_a_call(self) -> None:
        self.secrets.values[DESTINATION] = json.dumps({**CHOSEN, "project_key": "HR", "project_name": "HR"})
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "failed")
        self.assertIn("DestinationChanged", self.action().error)
        self.assertEqual(self.timeline, ["audit", "failed"])

    def test_without_destinations_a_jira_plan_is_refused(self) -> None:
        executor = WorkflowExecutor(self.processor, self.sessions, self.steps, CORPORATE,
                                    {"build_document": lambda a: None, "save_result": lambda a: None})
        with self.assertRaises(SendRefused):
            executor.execute("s1", "s1-1")
        self.assertEqual(self.atlassian.requests, [])


class StubAccount:
    def destination(self) -> dict[str, str]:
        return CHOSEN


class JiraDestinationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.export = Path(temp.name)

    def test_no_project_chosen_keeps_work_items_local(self) -> None:
        builder = desktop_payload_builder(self.export, calendar=False, jira=JiraAccount(None))
        action = ProposedAction("a", "T", "D", "Q", action_type="work_item", fields={})
        [step] = builder.build(action, CORPORATE, {})["steps"]
        self.assertEqual(step["connector"], "local_document")

    def test_medical_actions_never_go_to_jira(self) -> None:
        self.assertEqual(set(DESTINATIONS), {("corporate", "work_item")})
        builder = desktop_payload_builder(self.export, calendar=False, jira=StubAccount())
        for action_type in MEDICAL.action_types:
            steps = [s for s in action_type.workflow.steps if s.connector]
            if len(steps) == 1 and steps[0].connector == "local_document":
                action = ProposedAction("a", "T", "D", "Q", action_type=action_type.name,
                                        fields={f.name: "2026-11-20" if f.type == "date" else "10:00" if f.type == "time" else "x"
                                                for f in action_type.fields})
                with self.subTest(action_type=action_type.name):
                    [step] = builder.build(action, MEDICAL, {})["steps"]
                    self.assertEqual(step["connector"], "local_document")

    def test_builder_refuses_without_destination_or_with_options(self) -> None:
        action = ProposedAction("a", "T", "D", "Q", action_type="work_item", fields={})
        with self.assertRaises(UnsupportedWorkflow):
            JiraPayloadBuilder(lambda: None).build(action, CORPORATE, {})
        with self.assertRaises(ValidationError):
            JiraPayloadBuilder(lambda: CHOSEN).build(action, CORPORATE, {"x": 1})

    def test_a_jira_plan_for_an_action_type_without_destinations_is_not_approved(self) -> None:
        sessions = InMemorySessionStore()
        sessions.save_session(Session(id="s1", started_at=AT, domain="corporate"))
        action = ProposedAction("s1-1", "T", "D", "Q", action_type="work_item", fields={})
        revision = sessions.save_actions("s1", [action], expected_revision=0)
        with self.assertRaises(ValidationError):
            preview_action("s1", "s1-1", {}, expected_revision=revision, store=sessions, domain=CORPORATE,
                           builder=JiraPayloadBuilder(lambda: CHOSEN))  # no destinations given

    def test_build_jira_needs_an_oauth_app_and_an_encrypted_store(self) -> None:
        account, connector = build_jira(MemorySecrets(), JiraSettings("id", "secret"))
        self.assertEqual(account.status(), {"available": True, "connected": False, "destination": None})
        self.assertIsNotNone(connector)
        self.assertIsNone(build_jira(MemorySecrets(), JiraSettings(None, None))[1])
        self.assertIsNone(build_jira(object(), JiraSettings("id", "secret"))[1])


class JiraBridgeTest(unittest.TestCase):
    def test_the_demo_has_no_jira_account(self) -> None:
        from norev.ui.backend import DemoBackend
        from norev.ui.bridge import Bridge

        bridge = Bridge(DemoBackend(InMemorySessionStore(), approved_by="local:ana"))
        self.assertEqual(bridge.jira_status(), {"ok": True, "data": {"available": False, "connected": False, "destination": None}})
        for call in (bridge.jira_connect, bridge.jira_disconnect, bridge.jira_projects, lambda: bridge.jira_choose(CLOUD, "OPS")):
            self.assertEqual(call()["error"]["code"], "mode_unavailable")


if __name__ == "__main__":
    unittest.main()

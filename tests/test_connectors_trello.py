"""Trello (#72): the User's token and list, the Connector and its PayloadBuilder, against a fake
Trello on 127.0.0.1: no network."""
import json
import threading
import unittest
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from norev.adapters.connectors.http import HttpError, Unreachable
from norev.adapters.connectors.trello import (
    LIST,
    TOKEN,
    InvalidToken,
    NotConfigured,
    NotConnected,
    TrelloAccount,
    TrelloConnector,
    TrelloSettings,
    marker_of,
)
from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.core.models import ProposedAction, UnsupportedWorkflow, ValidationError
from norev.core.ports import Connector
from norev.processor.payloads import TrelloPayloadBuilder
from norev.usecases.approve import domain_digest
from test_connectors_google_auth import MemorySecrets

KEY = "k" * 32
GOOD = "ATTA" + "a1" * 32  # a token the fake accepts
SECRET = "Ion Popescu, CNP 1800101123456"
LIST_ID = "5f1a2b3c4d5e6f7a8b9c0d1e"
OTHER_LIST = "6a6a6a6a6a6a6a6a6a6a6a6a"
NL = chr(10)
CORPORATE = load_domain(DOMAINS / "corporate")


class FakeTrello:
    """members/me, boards with lists, list cards, card creation and token revocation."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, str]] = []  # method, path+query, body, Authorization
        self.cards: list[dict] = []
        self.fail_next: list[tuple[int, object]] = []
        self.lose_answer = False
        self.revoked: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                auth = self.headers.get("Authorization", "")
                fake.requests.append((method, self.path, body, auth))
                status, answer = fake.answer(method, self.path, body, auth)
                data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}/1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, method: str, path: str, body: dict | None, auth: str) -> tuple[int, object]:
        parts = urllib.parse.urlsplit(path)
        route = parts.path.removeprefix("/1")
        if f'oauth_consumer_key="{KEY}"' not in auth or f'oauth_token="{GOOD}"' not in auth:
            return 401, "invalid token"
        if self.fail_next:
            return self.fail_next.pop(0)
        if method == "GET" and route == "/members/me":
            return 200, {"id": "m1", "username": "ana_test"}
        if method == "GET" and route == "/members/me/boards":
            return 200, [{"id": "b1", "name": "Echipa", "lists": [{"id": LIST_ID, "name": "De făcut"},
                                                                    {"id": OTHER_LIST, "name": "Gata"}]}]
        if method == "GET" and route.startswith("/lists/") and route.endswith("/cards/all"):
            list_id = route.split("/")[2]
            return 200, [{"id": c["id"], "desc": c["desc"], "shortUrl": c["shortUrl"]} for c in self.cards
                         if c["idList"] == list_id]
        if method == "POST" and route == "/cards":
            card = {**body, "id": f"c{len(self.cards) + 1}" + "0" * 22, "shortUrl": f"https://trello.com/c/x{len(self.cards)}"}
            self.cards.append(card)
            if self.lose_answer:
                self.lose_answer = False
                return 503, "unavailable"
            return 200, card
        if method == "DELETE" and route == f"/tokens/{GOOD}":
            self.revoked.append(GOOD)
            return 200, {}
        return 404, "not found"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.trello = FakeTrello()
        self.addCleanup(self.trello.close)
        self.store = MemorySecrets()
        self.account = TrelloAccount(TrelloSettings(KEY), self.store, api=self.trello.api, timeout=5)

    def connected(self) -> None:
        self.store.values[TOKEN] = GOOD
        self.store.values[LIST] = json.dumps({"id": LIST_ID, "name": "Echipa / De făcut"})


class TrelloAccountTest(Case):
    def test_authorize_url_asks_read_write_for_30_days(self) -> None:
        url = urllib.parse.urlsplit(self.account.authorize_url())
        query = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "trello.com", "/1/authorize"))
        self.assertEqual(query, {"expiration": "30days", "name": "norev", "scope": "read,write",
                                 "response_type": "token", "key": KEY})

    def test_a_pasted_token_is_checked_then_kept(self) -> None:
        self.assertEqual(self.account.connect("  " + GOOD + NL), "ana_test")
        self.assertEqual(self.store.values, {TOKEN: GOOD})
        self.assertTrue(self.account.connected())
        [(method, path, _, auth)] = self.trello.requests
        self.assertEqual((method, path), ("GET", "/1/members/me?fields=username"))
        self.assertNotIn(GOOD, path)  # in the header, never in the URL
        self.assertIn(f'oauth_token="{GOOD}"', auth)

    def test_a_wrong_token_saves_nothing(self) -> None:
        for token in ("x" * 64, "short", "has space " * 8, 'quote"' * 8, None, "ș" * 40):
            with self.subTest(token=str(token)[:10]), self.assertRaises(InvalidToken):
                self.account.connect(token)
        self.assertEqual(self.store.values, {})
        self.assertEqual(len(self.trello.requests), 1)  # only the well-formed one was asked

    def test_an_unexpected_answer_saves_nothing(self) -> None:
        self.trello.fail_next = [(200, {"id": "m1"})]  # no username: not the answer of a valid token
        with self.assertRaises(HttpError):
            self.account.connect(GOOD)
        self.assertEqual(self.store.values, {})

    def test_boards_and_choosing_a_list(self) -> None:
        self.store.values[TOKEN] = GOOD
        self.assertEqual(self.account.boards(), [{"id": "b1", "name": "Echipa", "lists": [
            {"id": LIST_ID, "name": "De făcut"}, {"id": OTHER_LIST, "name": "Gata"}]}])
        self.assertEqual(self.account.choose_list(LIST_ID), {"id": LIST_ID, "name": "Echipa / De făcut"})
        self.assertEqual(self.account.chosen_list(), {"id": LIST_ID, "name": "Echipa / De făcut"})
        with self.assertRaises(LookupError):
            self.account.choose_list("0" * 24)

    def test_disconnect_revokes_and_deletes_token_and_list(self) -> None:
        self.connected()
        self.assertTrue(self.account.disconnect())
        self.assertEqual(self.trello.revoked, [GOOD])
        self.assertEqual(self.store.values, {})
        self.assertFalse(self.account.disconnect())

    def test_disconnect_deletes_locally_even_when_trello_fails(self) -> None:
        self.connected()
        self.trello.close()
        self.assertFalse(self.account.disconnect())
        self.assertEqual(self.store.values, {})

    def test_not_connected_and_not_configured(self) -> None:
        with self.assertRaises(NotConnected):
            self.account.boards()
        with self.assertRaises(NotConfigured):
            TrelloAccount(TrelloSettings(None), self.store).authorize_url()
        self.assertEqual(self.trello.requests, [])
        self.assertEqual(TrelloSettings.from_environment({"NOREV_TRELLO_API_KEY": "abc"}).api_key, "abc")
        self.assertIsNone(TrelloSettings.from_environment({}).api_key)

    def test_no_token_in_repr_or_errors(self) -> None:
        self.connected()
        self.trello.fail_next = [(500, {"error": f"boom {GOOD}"})]
        with self.assertRaises(HttpError) as caught:
            self.account.boards()
        self.assertNotIn(GOOD, str(caught.exception) + repr(self.account))


def card_payload(marker: str = "norev:" + "a" * 32, list_id: str = LIST_ID) -> dict:
    return {"list": {"id": list_id, "name": "Echipa / De făcut"},
            "card": {"name": "Send the Q4 report", "desc": f"Send it.{NL}{NL}{marker}", "due": "2026-11-13T12:00:00+02:00"}}


class TrelloConnectorTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.connected()
        self.connector = TrelloConnector(self.account)

    def test_creates_exactly_the_approved_card(self) -> None:
        self.assertIsInstance(self.connector, Connector)
        result = self.connector.send(card_payload())
        self.assertEqual(result, {"card_id": "c1" + "0" * 22, "created": True, "link": "https://trello.com/c/x0"})
        method, path, body, _ = self.trello.requests[-1]
        self.assertEqual((method, path), ("POST", "/1/cards"))
        self.assertEqual(body, {"idList": LIST_ID, "pos": "bottom", **card_payload()["card"]})

    def test_sending_again_finds_the_card_by_its_marker(self) -> None:
        self.connector.send(card_payload())
        again = self.connector.send(card_payload())
        self.assertEqual((again["card_id"], again["created"]), ("c1" + "0" * 22, False))
        self.assertEqual(len(self.trello.cards), 1)
        self.connector.send(card_payload(marker="norev:" + "b" * 32))  # another approved card: created
        self.assertEqual(len(self.trello.cards), 2)

    def test_retry_after_a_lost_answer_creates_one_card(self) -> None:
        self.trello.lose_answer = True
        with self.assertRaises(HttpError) as caught:
            self.connector.send(card_payload())
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(self.connector.send(card_payload())["created"])
        self.assertEqual(len(self.trello.cards), 1)

    def test_a_card_in_another_list_is_not_taken_for_ours(self) -> None:
        self.connector.send(card_payload(list_id=OTHER_LIST))
        self.assertTrue(self.connector.send(card_payload())["created"])

    def test_revoked_token_unreachable_and_not_connected(self) -> None:
        self.trello.fail_next = [(401, "invalid token")]
        with self.assertRaises(HttpError) as caught:
            self.connector.send(card_payload())
        self.assertEqual(caught.exception.status, 401)
        self.trello.fail_next = [(429, {"error": "API_TOKEN_LIMIT_EXCEEDED"})]
        with self.assertRaises(HttpError) as caught:
            self.connector.send(card_payload())
        self.assertEqual((caught.exception.status, caught.exception.error), (429, "API_TOKEN_LIMIT_EXCEEDED"))
        del self.store.values[TOKEN]
        with self.assertRaises(NotConnected):
            self.connector.send(card_payload())
        self.trello.close()
        self.store.values[TOKEN] = GOOD
        with self.assertRaises(Unreachable):
            self.connector.send(card_payload())

    def test_errors_carry_no_card_text(self) -> None:
        self.trello.fail_next = [(400, f"invalid value for desc: {SECRET}")]
        payload = card_payload()
        payload["card"]["desc"] = f"{SECRET}{NL}{NL}norev:" + "a" * 32
        with self.assertRaises(HttpError) as caught:
            self.connector.send(payload)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_payloads_not_shaped_like_the_builders_are_refused_without_a_call(self) -> None:
        good = card_payload()
        bad = [None, {"card": good["card"]}, {**good, "extra": 1},
               {**good, "list": {"id": "../members/me", "name": "x"}},
               {**good, "list": {"id": LIST_ID}},
               {**good, "card": {**good["card"], "idMembers": ["m1"]}},
               {**good, "card": {**good["card"], "pos": "top"}},
               {**good, "card": {**good["card"], "idLabels": "l1"}},
               {**good, "card": {**good["card"], "name": " "}},
               {**good, "card": {**good["card"], "desc": "no marker"}},
               {**good, "card": {**good["card"], "desc": "norev:" + "a" * 32 + NL + "after"}},
               {**good, "card": {**good["card"], "due": 5}}]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(ValueError):
                self.connector.send(payload)
        self.assertEqual(self.trello.requests, [])

    def test_marker_is_only_the_last_line(self) -> None:
        self.assertEqual(marker_of("text" + NL + NL + "norev:" + "c" * 32 + NL), "norev:" + "c" * 32)
        self.assertIsNone(marker_of("norev:" + "c" * 32 + NL + "text"))
        self.assertIsNone(marker_of("norev:XYZ"))
        self.assertIsNone(marker_of(None))


def work_item(**changes) -> ProposedAction:
    values = dict(id="s1-1", title="Send the Q4 report", description="Send the draft to the client.",
                  transcript_excerpt="can you send the Q4 report by Friday?", owner="Elena",
                  due_date=date(2026, 11, 13), action_type="work_item", fields={"deliverable": "the Q4 report"})
    return ProposedAction(**(values | changes))


class TrelloPayloadBuilderTest(unittest.TestCase):
    def builder(self, chosen=lambda: {"id": LIST_ID, "name": "Echipa / De făcut"}) -> TrelloPayloadBuilder:
        return TrelloPayloadBuilder(chosen, "Europe/Bucharest", new_id=lambda: "a" * 32)

    def test_exact_card(self) -> None:
        plan = self.builder().build(work_item(), self.trello_domain(), {})
        self.assertEqual(plan["domain_digest"], domain_digest(self.trello_domain()))
        [step] = plan["steps"]
        self.assertEqual((step["step"], step["connector"]), ("save_document", "trello"))
        self.assertEqual(step["payload"], {"list": {"id": LIST_ID, "name": "Echipa / De făcut"}, "card": {
            "name": "Send the Q4 report",
            "desc": NL.join(["Send the draft to the client.", "", "Responsabil: Elena", "", "Livrabil: the Q4 report", "",
                             "Din conversație: „can you send the Q4 report by Friday?”", "", "norev:" + "a" * 32]),
            "due": "2026-11-13T12:00:00+02:00",
        }})
        self.assertEqual(marker_of(step["payload"]["card"]["desc"]), "norev:" + "a" * 32)

    def test_summer_offset_and_no_due_date(self) -> None:
        plan = self.builder().build(work_item(due_date=date(2026, 7, 1)), self.trello_domain(), {})
        self.assertEqual(plan["steps"][0]["payload"]["card"]["due"], "2026-07-01T12:00:00+03:00")
        plan = self.builder().build(work_item(due_date=None, owner=None), self.trello_domain(), {})
        card = plan["steps"][0]["payload"]["card"]
        self.assertNotIn("due", card)
        self.assertNotIn("Responsabil", card["desc"])

    def test_no_list_chosen_blocks_the_preview(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.builder(lambda: None).build(work_item(), self.trello_domain(), {})
        self.assertEqual(caught.exception.code, "no_trello_list")

    def test_options_and_other_connectors_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.builder().build(work_item(), self.trello_domain(), {"list": OTHER_LIST})
        with self.assertRaises(UnsupportedWorkflow):  # the Domain's own step is local_document
            self.builder().build(work_item(), CORPORATE, {})

    @staticmethod
    def trello_domain():
        """Corporate as ConnectorPayloadBuilder shows it to the Trello builder: the step uses trello."""
        import dataclasses

        from norev.core.models import Workflow

        work = CORPORATE.action_type("work_item")
        steps = tuple(dataclasses.replace(s, connector="trello", alternatives=()) if s.connector else s
                      for s in work.workflow.steps)
        swapped = dataclasses.replace(work, workflow=Workflow(steps))
        return dataclasses.replace(CORPORATE, action_types=tuple(swapped if a.name == "work_item" else a
                                                                 for a in CORPORATE.action_types))


if __name__ == "__main__":
    unittest.main()

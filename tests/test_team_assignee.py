"""The assignee of a work item (ADR 0006): a Member's verified Jira or Trello id, in the approved payload."""
import json
import threading
import time
import unittest
import urllib.parse
import urllib.error
import urllib.request

from norev.adapters.connectors.jira import DESTINATION, IDENTITY_SCOPES, JiraSettings, _check_payload as check_jira
from norev.adapters.connectors.trello import TOKEN, AuthorizationFailed, TrelloAccount, TrelloSettings, _check_payload as check_trello
from norev.core.models import Member
from norev.processor.jira_account import JiraAccount
from norev.processor.payloads import JiraPayloadBuilder, TrelloPayloadBuilder
from norev.processor.trello_account import TrelloLink
from test_connectors_google_auth import MemorySecrets
from test_connectors_jira import CHOSEN, CLOUD, FakeAtlassian, _as_jira, free_port, payload
from test_connectors_jira import CORPORATE as JIRA_CORPORATE
from test_connectors_trello import GOOD, KEY, LIST_ID, FakeTrello, TrelloPayloadBuilderTest, card_payload, work_item

TRELLO_ID = "6b6b6b6b6b6b6b6b6b6b6b6b"
ACCOUNT = "712020:2c1e4f7a-0000-4b5c-9d8e-123456789abc"
ION = Member("ion@firma.ro", "Ion", ("backend",), trello_user="ion_t", trello_member_id=TRELLO_ID,
             jira_user="Ion Ionescu", jira_account_id=ACCOUNT)
UNVERIFIED = Member("dan@firma.ro", "Dan")  # never signed in to Jira or Trello
TEAM = {m.email: m for m in (ION, UNVERIFIED)}


class BuilderTest(unittest.TestCase):
    def trello(self, action):
        builder = TrelloPayloadBuilder(lambda: {"id": LIST_ID, "name": "L"}, "Europe/Bucharest",
                                       new_id=lambda: "a" * 32, member_of=TEAM.get)
        return builder.build(action, TrelloPayloadBuilderTest.trello_domain(), {})["steps"][0]["payload"]["card"]

    def jira(self, action):
        builder = JiraPayloadBuilder(lambda: CHOSEN, new_id=lambda: "a" * 32, member_of=TEAM.get)
        return builder.build(action, _as_jira(JIRA_CORPORATE), {})["steps"][0]["payload"]["issue"]["fields"]

    def test_a_verified_member_is_the_assignee(self):
        action = work_item(owner="Ion", assignee="ion@firma.ro")
        card = self.trello(action)
        self.assertEqual(card["idMembers"], TRELLO_ID)
        self.assertIn("Responsabil: Ion", card["desc"])
        check_trello({"list": {"id": LIST_ID, "name": "L"}, "card": card})  # the Connector takes it
        fields = self.jira(action)
        self.assertEqual(fields["assignee"], {"accountId": ACCOUNT})
        check_jira({**payload(), "issue": {"fields": fields}})

    def test_no_assignee_without_a_verified_id_or_without_one_at_all(self):
        for action in (work_item(owner="Dan", assignee="dan@firma.ro"), work_item(), work_item(assignee="gone@firma.ro")):
            self.assertNotIn("idMembers", self.trello(action))
            self.assertNotIn("assignee", self.jira(action))


class ConnectorCheckTest(unittest.TestCase):
    def test_a_malformed_assignee_is_refused(self):
        for bad in ("not-an-id", 7):
            card = {**card_payload()["card"], "idMembers": bad}
            with self.assertRaises(ValueError):
                check_trello({"list": {"id": LIST_ID, "name": "L"}, "card": card})
        for bad in ({"accountId": "a b"}, {"name": "ion"}, "ion", {"accountId": ACCOUNT, "x": 1}):
            with self.assertRaises(ValueError):
                check_jira(payload(assignee=bad))


BOARD = "7c7c7c7c7c7c7c7c7c7c7c7c"


class FakeTrelloMe(FakeTrello):
    def answer(self, method, path, body, auth):
        route = urllib.parse.urlsplit(path).path.removeprefix("/1")
        if method == "GET" and route == "/members/me" and f'oauth_token="{GOOD}"' in auth:
            return 200, {"id": TRELLO_ID, "username": "ion_t"}
        if method == "GET" and route == f"/lists/{LIST_ID}" and f'oauth_token="{GOOD}"' in auth:
            return 200, {"id": LIST_ID, "idBoard": BOARD}
        if method == "GET" and route == f"/boards/{BOARD}/members" and f'oauth_token="{GOOD}"' in auth:
            return 200, [{"id": TRELLO_ID, "username": "ion_t", "fullName": "Ion Ionescu"},
                         {"id": "bad", "username": "x"}, {"id": "8" * 24, "username": "dan_t", "fullName": ""}]
        return super().answer(method, path, body, auth)


class BoardMembersTest(unittest.TestCase):
    """The people on the chosen list's board, for the Team Leader to link Members (no emails from Trello)."""

    def test_the_board_of_the_chosen_list(self):
        trello = FakeTrelloMe()
        self.addCleanup(trello.close)
        secrets = MemorySecrets()
        link = TrelloLink(TrelloAccount(TrelloSettings(KEY), secrets, api=trello.api, timeout=5))
        self.assertEqual(link.board_members()["failed"], "not_connected")
        secrets.values[TOKEN] = GOOD
        self.assertEqual(link.board_members()["failed"], "no_such_list")  # connected, no list chosen yet
        secrets.values["trello.list"] = json.dumps({"id": LIST_ID, "name": "Echipa / De făcut"})
        self.assertEqual(link.board_members(), {"members": [
            {"id": TRELLO_ID, "username": "ion_t", "name": "Ion Ionescu"},
            {"id": "8" * 24, "username": "dan_t", "name": ""}], "failed": None})


class FakeAtlassianMe(FakeAtlassian):
    def answer(self, method, path, body, token):
        if urllib.parse.urlsplit(path).path == f"/ex/jira/{CLOUD}/rest/api/3/myself" and token in self.valid_tokens:
            return 200, {"accountId": ACCOUNT, "displayName": "Ion  Ionescu"}
        return super().answer(method, path, body, token)


class TrelloBrowserTest(unittest.TestCase):
    """Trello sign-in through the browser, which comes back to the app with the token: nothing to paste.
    The Team Leader's token is kept (sends); a Member's is read-only, one hour, and revoked at once."""

    def setUp(self):
        self.trello = FakeTrelloMe()
        self.addCleanup(self.trello.close)
        self.secrets = MemorySecrets()
        self.opened = []

    def account(self, token=GOOD, **back):
        def browser(url):  # the User allows norev; Trello sends the browser back with #token=...
            query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
            self.opened.append(query)

            def come_back():
                page = urllib.request.urlopen(query["return_url"], timeout=5).read().decode()
                self.relay = page
                state = urllib.parse.parse_qs(urllib.parse.urlsplit(query["return_url"]).query)["state"][0]
                base = query["return_url"].split("?")[0]
                try:
                    urllib.request.urlopen(urllib.request.Request(base + "token?state=" + back.get("state", state),
                                                                  data=token.encode(), method="POST"), timeout=5).read()
                except urllib.error.HTTPError:
                    pass  # a request without our state: refused, as it should be
            threading.Thread(target=come_back, daemon=True).start()
            return True
        return TrelloAccount(TrelloSettings(KEY, free_port()), self.secrets, api=self.trello.api, timeout=5, open_browser=browser)

    def test_a_member_is_identified_and_their_token_revoked(self):
        link = TrelloLink(self.account())
        self.assertEqual(link.identify_in_browser(), {"member_id": TRELLO_ID, "username": "ion_t", "failed": None})
        [query] = self.opened
        self.assertEqual((query["scope"], query["expiration"], query["callback_method"]), ("read", "1hour", "fragment"))
        self.assertTrue(query["return_url"].startswith("http://localhost:"))
        self.assertIn("location.hash", self.relay)  # the page that reads the token after "#"
        self.assertEqual(self.trello.revoked, [GOOD])
        self.assertEqual(self.secrets.values, {})  # nothing of the Member's sign-in is kept

    def test_the_team_leader_connects_without_pasting(self):
        status = TrelloLink(self.account()).connect_in_browser()
        self.assertEqual((status["failed"], status["connected"]), (None, True))
        self.assertEqual(self.opened[0]["scope"], "read,write")
        self.assertEqual(self.secrets.values, {TOKEN: GOOD})

    def test_a_new_sign_in_replaces_one_still_waiting(self):
        # the first browser shows Trello's error page and never comes back; the second one does
        attempts = []
        good = self.account()
        comes_back = good._open_browser
        good._open_browser = lambda url: attempts.append(url) or (True if len(attempts) == 1 else comes_back(url))
        link = TrelloLink(good)
        first = {}
        waiting = threading.Thread(target=lambda: first.update(link.identify_in_browser()))
        waiting.start()
        for _ in range(50):
            if attempts:
                break
            time.sleep(0.05)
        second = link.identify_in_browser()
        waiting.join(5)
        self.assertEqual(first["failed"], "cancelled")
        self.assertEqual(second, {"member_id": TRELLO_ID, "username": "ion_t", "failed": None})

    def test_a_refusal_or_a_foreign_request_brings_nothing(self):
        self.assertEqual(TrelloLink(self.account(token="")).identify_in_browser()["failed"], "access_denied")
        account = self.account(state="not-ours")
        with self.assertRaises(AuthorizationFailed):  # only our state counts; then it times out
            account.identify_in_browser(wait=1)
        self.assertEqual(TrelloLink(None).identify_in_browser()["failed"], "not_configured")


class JiraIdentityTest(unittest.TestCase):
    """A Member's Jira user from their own sign-in, on the Team Leader's site; nothing of it is kept."""

    def setUp(self):
        from test_connectors_jira import JiraAuthTest
        self.atlassian = FakeAtlassianMe()
        self.addCleanup(self.atlassian.close)
        self.secrets = MemorySecrets(**{DESTINATION: json.dumps(CHOSEN)})
        helper = JiraAuthTest()
        helper.atlassian, helper.secrets = self.atlassian, self.secrets
        helper.settings = JiraSettings("client-1", "secret-1", free_port())
        self.helper = helper

    def test_the_member_signs_in_and_only_their_account_id_is_learnt(self):
        self.atlassian.token_answers = [(200, {"access_token": "access-1", "expires_in": 3600, "scope": "read:jira-user"})]
        auth = self.helper.auth()
        self.assertEqual(auth.identify(wait=5), {"account_id": ACCOUNT, "name": "Ion Ionescu"})
        [query] = self.helper.opened
        self.assertEqual(query["scope"].split(), list(IDENTITY_SCOPES))
        self.assertEqual(self.secrets.values, {DESTINATION: json.dumps(CHOSEN)})  # no Member token kept

    def test_a_missing_scope_or_a_refusal_is_a_failure(self):
        self.atlassian.token_answers = [(200, {"access_token": "access-1", "expires_in": 3600, "scope": "read:jira-work"})]
        self.assertEqual(JiraAccount(self.helper.auth()).identify(wait=5), {"failed": "scope_not_granted"})
        self.assertEqual(JiraAccount(None).identify(), {"failed": "not_configured"})


if __name__ == "__main__":
    unittest.main()

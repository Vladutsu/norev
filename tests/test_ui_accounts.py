"""The sign-in gate (ADR 0006): the store opens only with a User's password; the Team's rights."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from norev.adapters.store.encrypted import EncryptedStore, create_key_file
from norev.adapters.store.slots import KeySlots
from norev.core.models import Session, ValidationError
from norev.ui.accounts import Gate, NotSignedIn, SignedInElsewhere
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge
from norev.usecases.team import NotTeamLeader

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
PASSWORD, OTHER = "parola-ana-1", "parola-ion-2"


class FakeBackend:
    """What the gate hands calls to once signed in; records what reached it."""

    def __init__(self, store, member):
        self.store, self.member = store, member
        self.calls, self.running = [], False
        self.can_execute = True

    def busy(self):
        return self.running

    def sessions(self):
        return [s.id for s in self.store.list_sessions()]

    def edit(self, *args):
        self.calls.append(("edit",) + args)
        return {"edited": args}

    def trello_connect(self, token):
        self.calls.append(("trello_connect", token))
        return {"connected": True}

    class jira:  # the Member's own sign-in (ADR 0006), as JiraAccount.identify answers
        answers = [{"account_id": "acc-ion", "name": "Ion Jira", "failed": None}]

        @classmethod
        def identify(cls):
            return cls.answers.pop(0)

    class trello:  # TrelloLink.identify_in_browser: the browser comes back with the token, nothing pasted
        @staticmethod
        def board_members():
            return {"members": [{"id": "6" * 24, "username": "ion_t", "name": "Ion Ionescu"}], "failed": None}

        answers = [{"failed": "access_denied"}, {"member_id": "6" * 24, "username": "ion_t", "failed": None}]

        @classmethod
        def identify_in_browser(cls):
            return cls.answers.pop(0)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.store_path = root / "norev.db"
        self.key_file = root / "keys" / "norev.key"
        self.slots = KeySlots(root / "keys" / "norev.slots", n=2 ** 10)
        self.now = T0
        self.built = []
        self.gate = self.make_gate()

    def make_gate(self, legacy=None):
        def build(store, member):
            self.built.append(FakeBackend(store, member))
            return self.built[-1]
        return Gate(store_path=self.store_path, slots=self.slots, build=build, legacy_key_file=legacy, now=lambda: self.now)

    def tearDown(self):
        try:
            self.gate.sign_out()
        except Exception:
            pass
        self.dir.cleanup()

    def found(self):
        return self.gate.sign_up(" Ana@Firma.ro ", "Ana", PASSWORD, "backend, sql")

    def test_first_sign_up_creates_the_store_and_the_team(self):
        status = self.gate.auth_status()
        self.assertEqual((status["first_run"], status["signed_in"], status["locked_out"]), (True, False, False))
        status = self.found()
        self.assertTrue(status["signed_in"])
        self.assertEqual(status["user"]["email"], "ana@firma.ro")
        self.assertTrue(status["user"]["leader"])
        self.assertTrue(self.store_path.is_file())
        self.assertFalse(self.key_file.exists())  # the key lives only in the slot
        self.assertEqual(self.built[-1].member.email, "ana@firma.ro")

    def test_sign_out_then_only_the_password_opens_it(self):
        self.found()
        self.gate.sign_out()
        self.assertFalse(self.gate.auth_status()["signed_in"])
        for email, password in (("ana@firma.ro", "gresita-1"), ("ion@firma.ro", PASSWORD), ("ana@firma.ro", None)):
            with self.assertRaises(ValidationError) as e:
                self.gate.sign_in(email, password)
            self.assertEqual(e.exception.code, "sign_in_failed")
        self.assertTrue(self.gate.sign_in("ANA@firma.ro", PASSWORD)["signed_in"])

    def test_nothing_else_answers_before_sign_in(self):
        with self.assertRaises(NotSignedIn):
            self.gate.sessions()
        with self.assertRaises(NotSignedIn):
            self.gate.team()
        self.assertFalse(self.gate.can_execute)
        bridge = Bridge(self.gate)
        self.assertEqual(bridge.sessions(), {"ok": False, "error": {"code": "signed_out", "field": ""}})
        self.assertEqual(bridge.review("s-1")["error"]["code"], "signed_out")
        self.assertEqual(bridge.execute("s-1", "a-1")["error"]["code"], "not_executable")

    def test_an_old_store_is_taken_over_with_its_key_file(self):
        create_key_file(self.key_file)
        old = EncryptedStore(self.store_path, self.key_file.read_bytes())
        old.save_session(Session(id="s-old", started_at=T0, domain="corporate"))
        old.close()
        self.gate = self.make_gate(legacy=self.key_file)
        self.assertTrue(self.gate.auth_status()["legacy_key"])
        self.found()
        self.assertEqual(self.built[-1].sessions(), ["s-old"])
        self.assertTrue(self.gate.legacy_key_delete()["legacy_key"] is False)
        self.assertFalse(self.key_file.exists())
        self.gate.sign_out()
        self.gate.sign_in("ana@firma.ro", PASSWORD)
        self.assertEqual(self.built[-1].sessions(), ["s-old"])

    def test_an_old_store_without_its_key_cannot_be_signed_up_into(self):
        EncryptedStore(self.store_path, os.urandom(32)).close()
        self.assertTrue(self.gate.auth_status()["locked_out"])
        with self.assertRaises(ValidationError) as e:
            self.found()
        self.assertEqual(e.exception.code, "store_without_key")
        self.assertTrue(self.slots.empty())

    def invite_ion(self):
        self.found()
        answer = self.gate.team_invite("ion@firma.ro")
        self.gate.sign_out()
        return answer["code"]

    def test_an_invited_member_signs_up_with_the_code_and_joins(self):
        code = self.invite_ion()
        self.assertFalse(self.gate.auth_status()["first_run"])
        with self.assertRaises(ValidationError) as e:
            self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "design", "AAAA-BBBB-CCCC")
        self.assertEqual(e.exception.code, "invalid_code")
        with self.assertRaises(ValidationError) as e:  # nobody signs up without an Invitation
            self.gate.sign_up("eve@firma.ro", "Eve", OTHER, "", code)
        self.assertEqual(e.exception.code, "invalid_code")
        status = self.gate.sign_up("ion@firma.ro", "Ion", OTHER, ["design"], code.lower().replace("-", " "))
        self.assertEqual((status["user"]["email"], status["user"]["leader"]), ("ion@firma.ro", False))
        team = self.gate.team()
        self.assertEqual([m["email"] for m in team["members"]], ["ana@firma.ro", "ion@firma.ro"])
        self.assertEqual(team["invitations"], [])  # a Member does not see them
        self.gate.sign_out()
        with self.assertRaises(ValidationError):  # the code is used up
            self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        self.gate.sign_in("ion@firma.ro", OTHER)

    def test_an_expired_code_is_refused(self):
        code = self.invite_ion()
        self.now = T0 + timedelta(days=7)
        with self.assertRaises(ValidationError) as e:
            self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        self.assertEqual(e.exception.code, "invitation_expired")

    def test_cancelled_invitation_and_removed_member_are_locked_out(self):
        self.found()
        code = self.gate.team_invite("ion@firma.ro")["code"]
        self.assertEqual(len(self.gate.team()["invitations"]), 1)
        self.gate.team_cancel_invitation("ion@firma.ro")
        self.gate.sign_out()
        with self.assertRaises(ValidationError):
            self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        code = self.invite_ion_again()
        self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        self.gate.sign_out()
        self.gate.sign_in("ana@firma.ro", PASSWORD)
        self.gate.team_remove("ion@firma.ro")
        self.gate.sign_out()
        with self.assertRaises(ValidationError) as e:
            self.gate.sign_in("ion@firma.ro", OTHER)
        self.assertEqual(e.exception.code, "sign_in_failed")

    def invite_ion_again(self):
        self.gate.sign_in("ana@firma.ro", PASSWORD)
        code = self.gate.team_invite("ion@firma.ro")["code"]
        self.gate.sign_out()
        return code

    def test_team_leader_rights(self):
        code = self.invite_ion()
        self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "design", code)
        for call in (lambda: self.gate.team_invite("x@firma.ro"), lambda: self.gate.trello_connect("tok"),
                     lambda: self.gate.team_remove("ana@firma.ro"),
                     lambda: self.gate.edit("s-1", "a-1", {"assignee": "ion@firma.ro"}, 0)):
            with self.assertRaises(NotTeamLeader):
                call()
        self.assertEqual(Bridge(self.gate).trello_connect("tok")["error"]["code"], "not_team_leader")
        self.gate.edit("s-1", "a-1", {"title": "x"}, 0)  # editing the rest is everyone's
        self.gate.sign_out()
        self.gate.sign_in("ana@firma.ro", PASSWORD)
        self.gate.trello_connect("tok")
        self.gate.edit("s-1", "a-1", {"assignee": "ION@firma.ro"}, 3)
        self.assertEqual(self.built[-1].calls[-1], ("edit", "s-1", "a-1", {"assignee": "ion@firma.ro", "owner": "Ion"}, 3))
        self.gate.edit("s-1", "a-1", {"assignee": None}, 4)
        self.assertEqual(self.built[-1].calls[-1][3], {"assignee": None, "owner": None})
        with self.assertRaises(ValidationError):
            self.gate.edit("s-1", "a-1", {"assignee": "strain@x.ro"}, 5)

    def test_profile_and_own_jira_and_trello_users_from_signing_in(self):
        self.found()
        me = lambda t: next(m for m in t["members"] if m["email"] == "ana@firma.ro")  # noqa: E731
        team = self.gate.profile_save("Ana P", "go, rust")
        self.assertEqual((me(team)["name"], me(team)["skills"], me(team)["jira_user"]), ("Ana P", ["go", "rust"], ""))
        FakeBackend.jira.answers[:] = [{"failed": "access_denied"}, {"account_id": "acc-ana", "name": "Ana Jira", "failed": None}]
        self.assertEqual(self.gate.profile_jira_connect()["failed"], "access_denied")
        team = self.gate.profile_jira_connect()
        self.assertEqual((team["failed"], me(team)["jira_user"]), (None, "Ana Jira"))
        self.assertEqual(self.built[-1].store.get_member("ana@firma.ro").jira_account_id, "acc-ana")
        self.assertEqual(self.gate.profile_trello_connect()["failed"], "access_denied")
        self.assertEqual(me(self.gate.profile_trello_connect())["trello_user"], "ion_t")
        self.assertEqual(me(self.gate.profile_unlink("jira"))["jira_user"], "")
        self.assertIsNone(self.built[-1].store.get_member("ana@firma.ro").jira_account_id)

    def test_the_team_leader_says_who_a_member_is_on_the_trello_board(self):
        code = self.invite_ion()
        self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        with self.assertRaises(NotTeamLeader):
            self.gate.team_trello_members()
        with self.assertRaises(NotTeamLeader):
            self.gate.team_link_trello("ion@firma.ro", "6" * 24)
        self.gate.sign_out()
        self.gate.sign_in("ana@firma.ro", PASSWORD)
        self.assertEqual(self.gate.team_trello_members()["members"][0]["username"], "ion_t")
        with self.assertRaises(ValidationError) as e:  # only someone on the board
            self.gate.team_link_trello("ion@firma.ro", "9" * 24)
        self.assertEqual(e.exception.code, "not_on_board")
        team = self.gate.team_link_trello("ION@firma.ro", "6" * 24)
        ion = next(m for m in team["members"] if m["email"] == "ion@firma.ro")
        self.assertEqual((ion["trello_user"], ion["trello_id"]), ("ion_t", "6" * 24))
        self.assertEqual(self.built[-1].store.get_member("ion@firma.ro").trello_member_id, "6" * 24)
        team = self.gate.team_link_trello("ion@firma.ro", None)
        self.assertIsNone(next(m for m in team["members"] if m["email"] == "ion@firma.ro")["trello_id"])

    def test_sign_out_waits_for_a_running_session(self):
        self.found()
        self.built[-1].running = True
        with self.assertRaises(SignedInElsewhere):
            self.gate.sign_out()
        self.built[-1].running = False
        self.gate.sign_out()

    def test_a_signed_up_email_cannot_sign_up_again(self):
        code = self.invite_ion()
        self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        self.gate.sign_out()
        with self.assertRaises(ValidationError) as e:
            self.gate.sign_up("ion@firma.ro", "Ion", OTHER, "", code)
        self.assertEqual(e.exception.code, "already_member")

    def test_values_are_checked_before_anything_is_written(self):
        with self.assertRaises(ValidationError) as e:
            self.gate.sign_up("ana@firma.ro", "Ana", "scurta", "")
        self.assertEqual(e.exception.code, "weak_password")
        self.assertFalse(self.store_path.exists())
        self.assertTrue(self.slots.empty())

    def test_the_demo_has_no_users(self):
        from norev.testing.fakes import InMemorySessionStore
        bridge = Bridge(DemoBackend(InMemorySessionStore(), approved_by="demo"))
        self.assertEqual(bridge.auth_status()["data"]["accounts"], False)
        self.assertEqual(bridge.team()["error"]["code"], "signed_out")


if __name__ == "__main__":
    unittest.main()

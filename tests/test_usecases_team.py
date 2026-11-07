"""The one local Team (ADR 0006): store contract on both stores, and the rules of who changes what."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from norev.adapters.store.encrypted import EncryptedStore
from norev.core.models import Invitation, Member, Team, ValidationError
from norev.core.ports import TeamStore
from norev.testing.fakes import InMemorySessionStore
from norev.usecases import team

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
SECRET = "SENTINEL-ana.popescu@firma.ro"


class TeamStoreContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_is_a_team_store(self):
        self.assertIsInstance(self.store, TeamStore)

    def test_members_by_email_any_case(self):
        self.assertEqual(self.store.list_members(), [])
        ana = Member("ana@firma.ro", "Ana", ("backend", "SQL"), leader=True, jira_user="ana", jira_account_id="acc-1")
        self.store.save_member(ana)
        self.store.save_member(Member("ion@firma.ro", "Ion"))
        self.assertEqual(self.store.get_member("ANA@firma.ro "), ana)
        self.assertEqual([m.name for m in self.store.list_members()], ["Ana", "Ion"])
        self.store.delete_member("ion@firma.ro")
        self.assertIsNone(self.store.get_member("ion@firma.ro"))

    def test_team_and_invitations(self):
        self.assertIsNone(self.store.get_team())
        self.store.save_team(Team("Platforma"))
        self.assertEqual(self.store.get_team(), Team("Platforma"))
        invitation = Invitation("ion@firma.ro", NOW + timedelta(days=7))
        self.store.save_invitation(invitation)
        self.assertEqual(self.store.list_invitations(), [invitation])
        self.store.delete_invitation("ion@firma.ro")
        self.assertEqual(self.store.list_invitations(), [])


class InMemoryTeamStoreTest(TeamStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemorySessionStore()


class EncryptedTeamStoreTest(TeamStoreContract, unittest.TestCase):
    def make_store(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "norev.db"
        return EncryptedStore(self.path, os.urandom(32))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_no_email_or_skill_in_clear_on_disk(self):
        self.store.save_member(Member(SECRET, "Ana Popescu", ("SENTINEL-skill",)))
        self.store.save_invitation(Invitation("SENTINEL-ion@firma.ro", NOW))
        raw = self.path.read_bytes()
        for text in (SECRET, "Ana Popescu", "SENTINEL-skill", "SENTINEL-ion"):
            self.assertNotIn(text.encode(), raw)


class TeamRulesTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        self.leader = team.found_team(self.store, " Ana@Firma.ro", "  Ana   Popescu ", "backend, SQL, sql, ")

    def test_the_first_user_founds_the_team_once(self):
        self.assertEqual(self.leader, Member("ana@firma.ro", "Ana Popescu", ("backend", "SQL"), leader=True))
        self.assertEqual(self.store.get_team(), Team("Echipa"))
        with self.assertRaises(ValueError):
            team.found_team(self.store, "ion@firma.ro", "Ion", "")

    def test_invite_join_and_the_invitation_is_used_up(self):
        invitation = team.invite(self.store, "ana@firma.ro", "Ion@Firma.ro", NOW)
        self.assertEqual(invitation, Invitation("ion@firma.ro", NOW + timedelta(days=7)))
        ion = team.join(self.store, "ion@firma.ro", "Ion", ["design"], NOW + timedelta(days=6))
        self.assertFalse(ion.leader)
        self.assertEqual(self.store.list_invitations(), [])
        self.assertEqual(self.store.get_member("ion@firma.ro"), ion)
        with self.assertRaises(ValidationError) as e:  # used up
            team.join(self.store, "ion@firma.ro", "Ion", [], NOW)
        self.assertEqual(e.exception.code, "no_invitation")

    def test_an_expired_invitation_cannot_be_used(self):
        team.invite(self.store, "ana@firma.ro", "ion@firma.ro", NOW)
        with self.assertRaises(ValidationError) as e:
            team.join(self.store, "ion@firma.ro", "Ion", [], NOW + timedelta(days=7))
        self.assertEqual(e.exception.code, "invitation_expired")

    def test_only_the_team_leader_invites_removes_and_renames(self):
        team.invite(self.store, "ana@firma.ro", "ion@firma.ro", NOW)
        team.join(self.store, "ion@firma.ro", "Ion", [], NOW)
        for call in (lambda: team.invite(self.store, "ion@firma.ro", "x@firma.ro", NOW),
                     lambda: team.remove_member(self.store, "ion@firma.ro", "ana@firma.ro"),
                     lambda: team.rename_team(self.store, "ion@firma.ro", "Alta"),
                     lambda: team.cancel_invitation(self.store, "ion@firma.ro", "x@firma.ro")):
            with self.assertRaises(team.NotTeamLeader):
                call()
        with self.assertRaises(ValidationError):  # the Team Leader cannot remove themselves
            team.remove_member(self.store, "ana@firma.ro", "ana@firma.ro")
        team.remove_member(self.store, "ana@firma.ro", "ion@firma.ro")
        self.assertIsNone(self.store.get_member("ion@firma.ro"))
        self.assertEqual(team.rename_team(self.store, "ana@firma.ro", " Platforma "), Team("Platforma"))

    def test_a_member_cannot_be_invited_again(self):
        with self.assertRaises(ValidationError) as e:
            team.invite(self.store, "ana@firma.ro", "ana@firma.ro", NOW)
        self.assertEqual(e.exception.code, "already_member")

    def test_profile_and_linked_users(self):
        m = team.update_profile(self.store, "ana@firma.ro", name="Ana P", skills="go")
        self.assertEqual((m.name, m.skills), ("Ana P", ("go",)))
        team.link(self.store, "ana@firma.ro", "jira", "acc-1", "Ana Popescu")
        m = team.link(self.store, "ana@firma.ro", "trello", "tr-1", "ana_t")
        self.assertEqual((m.jira_account_id, m.jira_user, m.trello_member_id, m.trello_user), ("acc-1", "Ana Popescu", "tr-1", "ana_t"))
        m = team.link(self.store, "ana@firma.ro", "jira", None)
        self.assertEqual((m.jira_account_id, m.jira_user, m.trello_member_id), (None, "", "tr-1"))
        with self.assertRaises(ValidationError):
            team.link(self.store, "ana@firma.ro", "github", "x")

    def test_values_are_checked(self):
        for bad in ("", "fara-aroba", "a@b", "x" * 250 + "@a.ro", None):
            with self.assertRaises(ValidationError):
                team.check_email(bad)
        with self.assertRaises(ValidationError):
            team.check_password("scurta")
        with self.assertRaises(ValidationError):
            team.check_skills(["x" * 41])
        with self.assertRaises(ValidationError):
            team.check_skills([f"s{i}" for i in range(31)])
        with self.assertRaises(ValidationError):
            team.check_name("\x00rau")

    def test_codes(self):
        code = team.new_code()
        self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
        self.assertNotEqual(code, team.new_code())
        self.assertEqual(team.normal_code(" k7qf 2mxp-9tra "), "K7QF-2MXP-9TRA")


if __name__ == "__main__":
    unittest.main()

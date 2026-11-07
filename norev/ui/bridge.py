"""The pywebview js_api (#27): the only Python that JavaScript can call.

Arguments and results are JSON values. The backend validates and decides; the UI only shows what
it returns. No method here holds a Connector: execute, retry and reconcile reach the Workflow executor,
which sends only an approved payload through the Central Processor (ADR 0002, #22/#23, #43).
Errors come back as {"ok": false, "error": {"code", "field"}}, never with Session content.
"""
import logging
from typing import Any, Callable

from norev.core.models import InvalidTransition, StaleRevision, UnsupportedWorkflow, ValidationError
from norev.launcher.llama import LlamaError
from norev.processor.workflow import AlreadyRunning, ExecutionRefused
from norev.ui.accounts import NotSignedIn, SignedInElsewhere
from norev.ui.backend import DemoBackend, ExecutionPending, ModeUnavailable, PromptNotConfirmed
from norev.ui.session_backend import MicrophoneUnavailable, SessionBusy
from norev.usecases.team import NotTeamLeader

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, backend: DemoBackend) -> None:
        self._backend = backend

    # sign-in and the Team (ADR 0006); the demo has no Users, so it answers {"accounts": false}
    def auth_status(self): return self._call(self._backend.auth_status)
    def sign_up(self, email, name, password, skills, code=""): return self._call(self._team("sign_up"), email, name, password, skills, code)
    def sign_in(self, email, password): return self._call(self._team("sign_in"), email, password)
    def sign_out(self): return self._call(self._team("sign_out"))
    def team(self): return self._call(self._team("team"))
    def team_invite(self, email): return self._call(self._team("team_invite"), email)
    def team_cancel_invitation(self, email): return self._call(self._team("team_cancel_invitation"), email)
    def team_remove(self, email): return self._call(self._team("team_remove"), email)
    def team_rename(self, name): return self._call(self._team("team_rename"), name)
    def profile_save(self, name, skills): return self._call(self._team("profile_save"), name, skills)
    def profile_jira_connect(self): return self._call(self._team("profile_jira_connect"))
    def profile_trello_connect(self): return self._call(self._team("profile_trello_connect"))
    def profile_unlink(self, service): return self._call(self._team("profile_unlink"), service)
    def team_trello_members(self): return self._call(self._team("team_trello_members"))
    def team_link_trello(self, email, member_id): return self._call(self._team("team_link_trello"), email, member_id)
    def legacy_key_delete(self): return self._call(self._team("legacy_key_delete"))
    def modes(self): return self._call(self._backend.modes)
    def sessions(self): return self._call(self._backend.sessions)
    def others_completion_prompt(self, completion): return self._call(self._backend.others_completion_prompt, completion)
    def others_generate(self, context, focus=""): return self._call(self._backend.others_generate, context, focus)
    def others_confirm(self, context, prompt, focus="", completion=""):
        return self._call(self._backend.others_confirm, context, prompt, focus, completion)
    def start(self, mode, language, contact, others=None): return self._call(self._backend.start, mode, language, contact, others)
    def import_example(self, mode, language, contact, others=None): return self._call(self._backend.import_example, mode, language, contact, others)
    def import_file(self, mode, language, contact, others=None): return self._call(self._backend.import_file, mode, language, contact, others)
    def progress(self, session_id): return self._call(self._backend.progress, session_id)
    def delete(self, session_id): return self._call(self._backend.delete, session_id)
    def live(self, session_id): return self._call(self._backend.live, session_id)
    def pause(self, session_id): return self._call(self._backend.pause, session_id)
    def stop(self, session_id): return self._call(self._backend.stop, session_id)
    def review(self, session_id): return self._call(self._backend.review, session_id)
    def set_role(self, session_id, speaker_id, role): return self._call(self._backend.set_role, session_id, speaker_id, role)
    def edit_transcript(self, session_id, index, text): return self._call(self._backend.edit_transcript, session_id, index, text)
    def regenerate(self, session_id): return self._call(self._backend.regenerate, session_id)
    def edit(self, session_id, action_id, changes, revision): return self._call(self._backend.edit, session_id, action_id, changes, revision)
    def preview(self, session_id, action_id, revision, destination=None): return self._call(self._backend.preview, session_id, action_id, revision, destination)
    def approve(self, session_id, action_id, revision): return self._call(self._backend.approve, session_id, action_id, revision)
    def reject(self, session_id, action_id, revision): return self._call(self._backend.reject, session_id, action_id, revision)
    def execute(self, session_id, action_id): return self._call(self._executing("execute"), session_id, action_id)
    def retry(self, session_id, action_id): return self._call(self._executing("retry"), session_id, action_id)
    def reconcile(self, session_id, action_id, step, happened): return self._call(self._executing("reconcile"), session_id, action_id, step, happened)
    def open_draft(self, session_id, action_id): return self._call(self._executing("open_draft"), session_id, action_id)
    def completion_preview(self, session_id): return self._call(self._executing("completion_preview"), session_id)
    def completion_save(self, session_id, payload): return self._call(self._executing("completion_save"), session_id, payload)
    def google_status(self): return self._call(self._backend.google_status)
    def trello_status(self): return self._call(self._backend.trello_status)
    def trello_authorize(self): return self._call(self._backend.trello_authorize)
    def trello_connect(self, token): return self._call(self._backend.trello_connect, token)
    def trello_sign_in(self): return self._call(self._backend.trello_sign_in)
    def trello_set_key(self, key): return self._call(self._backend.trello_set_key, key)
    def trello_boards(self): return self._call(self._backend.trello_boards)
    def trello_choose_list(self, list_id): return self._call(self._backend.trello_choose_list, list_id)
    def trello_disconnect(self): return self._call(self._backend.trello_disconnect)
    def jira_status(self): return self._call(self._backend.jira_status)
    def jira_connect(self): return self._call(self._backend.jira_connect)
    def jira_disconnect(self): return self._call(self._backend.jira_disconnect)
    def jira_projects(self): return self._call(self._backend.jira_projects)
    def jira_choose(self, cloud_id, project_key): return self._call(self._backend.jira_choose, cloud_id, project_key)
    def google_connect(self): return self._call(self._backend.google_connect)
    def google_disconnect(self): return self._call(self._backend.google_disconnect)

    def _team(self, name: str) -> Callable[..., Any]:
        """A sign-in or Team call; a backend without Users (the demo) refuses it."""
        method = getattr(self._backend, name, None)
        if method is not None:
            return method

        def unavailable(*args: Any) -> Any:
            raise NotSignedIn()
        return unavailable

    def _executing(self, name: str) -> Callable[..., Any]:
        if self._backend.can_execute:
            return getattr(self._backend, name)

        def refused(*args: Any) -> Any:
            raise ExecutionRefused("the demo does not execute")
        return refused

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "data": fn(*args)}
        except ValidationError as e:
            return _error(e.code, e.field)
        except StaleRevision:
            return _error("stale", "revision")
        except InvalidTransition:
            return _error("already_decided", "status")
        except PromptNotConfirmed:
            return _error("prompt_not_confirmed", "prompt")
        except ModeUnavailable:
            return _error("mode_unavailable", "mode")
        except UnsupportedWorkflow:
            return _error("unsupported_workflow", "workflow")
        except SessionBusy:
            return _error("session_busy", "session")
        except ExecutionPending:
            return _error("execution_pending", "session")
        except MicrophoneUnavailable:
            return _error("no_microphone", "device")
        except NotSignedIn:
            return _error("signed_out", "")
        except NotTeamLeader:
            return _error("not_team_leader", "")
        except SignedInElsewhere:
            return _error("session_busy", "session")
        except AlreadyRunning:
            return _error("already_running", "action")
        except ExecutionRefused:
            return _error("not_executable", "action")
        except LlamaError:
            return _error("llm_unavailable", "")
        except LookupError:
            return _error("not_found", "session")
        except ValueError:
            return _error("invalid", "")
        except Exception as e:  # never forward the message: it can quote Session content
            log.error("UI call %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return _error("internal", "")


def _error(code: str, field: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "field": field}}

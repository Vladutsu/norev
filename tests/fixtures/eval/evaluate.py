"""Evaluation of the generic extraction against expected results checked by a person (#25).

    python tests/fixtures/eval/evaluate.py record           # GPU laptop: llama-server; rewrites the recordings
    python tests/fixtures/eval/evaluate.py record-domain    # GPU laptop: llama-server; rewrites only domain.neutral.json
                                                            # (a Domain prompt changed; translation and extraction kept)
    python tests/fixtures/eval/evaluate.py variance [N]     # GPU laptop: N more extract runs per variant (default 2)
    python tests/fixtures/eval/evaluate.py check            # no model: replays them, rebuilds report.json and compares
    python tests/fixtures/eval/evaluate.py refresh          # no model: replays them and rewrites results and report
                                                            # (after a code change that keeps the prompts)
    python tests/fixtures/eval/evaluate.py report           # prints report.json as a table

Each <session>/ holds the Transcript (or points to one in ../llm), expected.json (written and checked
by a person, never copied from the model's answer) and the recordings:
  translation.json        the translate calls and the English Transcript (shared by both variants)
  extract.<variant>.json  the extract calls and the Proposed Actions they gave
  domain.neutral.json     Corporate only: the Speaker Roles call and the Domain stage call on the neutral
                          result, as run_session makes them (#60), and the typed Proposed Actions
                          (action_type, fields, owner Role) they gave (#46)
and next to them variance.json: the Proposed Actions of extra runs on the same English Transcript.

Variants, on the same English Transcript:
  neutral   the generic extraction as the pipeline runs it (Segment texts only)
  speakers  the same extraction with "[S1] " in front of each Segment, to see whether Speakers help

The report counts omissions, invented actions, wrong or missing owners and dates, cancelled ideas
listed as actions, and numbers changed by the translation. A model mistake stays in the report:
the expected results are not changed to make a case pass. Thresholds: thresholds.json.
Run from the repo root.
"""
import dataclasses
import json
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2]))
sys.path.insert(0, str(HERE.parent / "llm"))

import record  # noqa: E402  (tests/fixtures/llm/record.py: same settings and record/replay clients)
from LLM.AI_Model.client import build_llm_client  # noqa: E402
from LLM.AI_Model.settings import DeepSeekSettings  # noqa: E402
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor  # noqa: E402
from norev.adapters.llm.translator import TurnTranslator  # noqa: E402
from norev.adapters.domains.loader import load_domain  # noqa: E402
from norev.core.models import Speaker, Transcript  # noqa: E402
from norev.core.speakers import speakers_in  # noqa: E402
from norev.usecases.domain_stage import ask_domain  # noqa: E402
from norev.usecases.extract import to_proposed_actions  # noqa: E402
from norev.usecases import speaker_roles  # noqa: E402

DOMAIN_STAGE = ("corporate",)  # Domains whose stage is evaluated; Medical is an example, Others needs the User's prompt

VARIANTS = ("neutral", "speakers")
_TITLES = re.compile(r"\b(mrs|mr|ms|dr|doamna|domnul|doctor's)\.?\s+", re.I)


# ── running the pipeline ────────────────────────────────────────────────────────

def sessions() -> list[Path]:
    return sorted(p for p in HERE.iterdir() if (p / "expected.json").exists())


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def source_of(folder: Path) -> dict:
    expected = _json(folder / "expected.json")
    return _json((folder / expected.get("transcript", "transcript.json")).resolve())


def with_speakers(english: Transcript) -> Transcript:
    """The speakers variant: the Speaker id in front of each Segment's text (no names, no Roles)."""
    return dataclasses.replace(english, segments=tuple(
        dataclasses.replace(s, text=f"[{s.speaker_id}] {s.text}" if s.speaker_id else s.text) for s in english.segments
    ))


def translate(source: dict, client, settings) -> Transcript:
    client.stage = "translate"
    return TurnTranslator(client, model=settings.model).translate(record.load_transcript(source), source["language"])


def extract(english: Transcript, variant: str, source: dict, client, settings) -> dict:
    client.stage = "extract"
    transcript = with_speakers(english) if variant == "speakers" else english
    return LegacyActionExtractor(client=client, settings=settings).extract(transcript, date.fromisoformat(source["session_date"]))


def domain_stage(english: Transcript, result: dict, source: dict, client) -> dict:
    """Speaker Roles, then the Corporate Domain stage on the neutral generic result, as run_session
    runs them (#60): same prompts, the Domain's model, an invalid Roles answer leaves them unknown."""
    domain = load_domain(HERE.parents[2] / "domains" / source["domain"])
    actions = to_proposed_actions(result, id_prefix="pa")
    speakers = speakers_in(english)
    if speakers:  # propose_speaker_roles without a store: the same calls, nothing saved
        client.stage = "roles"
        raw = client.generate_json(speaker_roles.build_prompt(domain, english), model=domain.model,
                                   json_schema=speaker_roles.answer_schema(domain, speakers))
        roles = speaker_roles.roles_from_answer(raw, domain, speakers)
        speakers = [Speaker(s.id, roles.get(s.id)) for s in speakers]
    client.stage = "domain"
    typed, suggestions = ask_domain(domain, client, english, actions, date.fromisoformat(source["session_date"]), speakers)
    return {
        "speaker_roles": {s.id: s.role for s in speakers},
        "actions": [{"id": a.id, "title": a.title, "action_type": a.action_type, "fields": a.fields, "owner": a.owner}
                    for a in typed],
        "suggestions": [s.text for s in suggestions],
    }


# ── scoring ─────────────────────────────────────────────────────────────────────

def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def _matches(action: dict, groups: list[list[str]]) -> bool:
    text = _plain(" ".join(str(action.get(k) or "") for k in ("title", "description", "transcript_excerpt")))
    return all(any(_plain(word) in text for word in group) for group in groups)


def _owner_ok(owner, accepted) -> bool | None:
    """True if accepted, False if wrong, None if missing (no owner where a person was expected)."""
    if accepted == "any":
        return True
    owner = _plain(_TITLES.sub("", owner or ""))
    if not owner:
        return True if None in accepted else None
    return any(a is not None and re.search(rf"\b{re.escape(_plain(a))}\b", owner) for a in accepted)


def _due_ok(due, accepted) -> bool | None:
    if accepted == "any":
        return True
    due = due or None
    if due is None:
        return True if None in accepted else None
    return due in accepted


_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_NUMBER_WORD = re.compile(r"\b(?:a|and|hundred|thousand|" + "|".join(list(_UNITS) + list(_TENS)) + r")(?:[\s-]+(?:and|hundred|thousand|"
                          + "|".join(list(_UNITS) + list(_TENS)) + r"))*\b")


def _words_to_digits(text: str) -> str:
    """"two hundred ten" -> "210", "thirty-eight" -> "38", "a thousand" -> "1000": the translation may spell numbers."""
    def convert(match: re.Match) -> str:
        words = [w for w in re.split(r"[\s-]+", match.group(0)) if w]
        total = current = 0
        seen_number = False
        for w in words:
            if w in _UNITS:
                current += _UNITS[w]; seen_number = True
            elif w in _TENS:
                current += _TENS[w]; seen_number = True
            elif w == "hundred":
                current = max(current, 1) * 100; seen_number = True
            elif w == "thousand":
                total += max(current, 1) * 1000; current = 0; seen_number = True
        if not seen_number:
            return match.group(0)
        return str(total + current)
    return _NUMBER_WORD.sub(convert, text)


def _number_kept(text: str, english: list[str]) -> bool:
    text = text.lower()
    text = text + " " + _words_to_digits(text)
    for variant in english:
        if re.fullmatch(r"[0-9][0-9.,:/]*", variant):
            if re.search(rf"(?<![0-9]){re.escape(variant)}(?![0-9])", text):
                return True
        elif variant.lower() in text:
            return True
    return False


def score(expected: dict, actions: list[dict], english_text: str, typed: list[dict] | None = None) -> dict:
    """Per-Session counts. Expected actions are matched most specific first, each to one produced action.
    `typed`: the Domain stage result (ids pa-1, pa-2... in the order of `actions`), when it ran."""
    pool = list(range(len(actions)))
    found: dict[str, dict] = {}
    for exp in sorted(expected["actions"], key=lambda e: -len(e["match"])):
        hit = next((i for i in pool if _matches(actions[i], exp["match"])), None)
        if hit is None:
            continue
        pool.remove(hit)
        found[exp["key"]] = {
            "index": hit,
            "owner": _owner_ok(actions[hit].get("owner"), exp["owner"]),
            "due": _due_ok(actions[hit].get("due_date"), exp["due"]),
            "got": {"title": actions[hit].get("title"), "owner": actions[hit].get("owner"), "due_date": actions[hit].get("due_date")},
        }
    required = [e["key"] for e in expected["actions"] if not e.get("optional")]
    leftover = [actions[i] for i in pool]
    cancelled = [n["key"] for n in expected.get("not_actions", []) if any(_matches(a, n["match"]) for a in leftover)]
    numbers = {n["key"]: _number_kept(english_text, n["english"]) for n in expected.get("numbers", [])}
    result = {
        "expected": len(required),
        "produced": len(actions),
        "omitted": sorted(k for k in required if k not in found),
        "invented": sorted(str(a.get("title")) for a in leftover),
        "cancelled_listed": sorted(cancelled),
        "owner_wrong": sorted(k for k, f in found.items() if f["owner"] is False),
        "owner_missing": sorted(k for k, f in found.items() if f["owner"] is None),
        "date_wrong": sorted(k for k, f in found.items() if f["due"] is False),
        "date_missing": sorted(k for k, f in found.items() if f["due"] is None),
        "found": len([k for k in required if k in found]),
        "matched": len(found),
        "numbers": len(numbers),
        "numbers_changed": sorted(k for k, kept in numbers.items() if not kept),
        "details": found,
    }
    if typed is not None:
        result["domain"] = _score_domain(expected, actions, found, typed)
    typed_keys = {e["key"] for e in expected["actions"] if "action_type" in e}

    def solved(case: dict) -> bool | None:
        if "number" in case:
            return case["number"] not in result["numbers_changed"]
        if case["action"] in typed_keys and "domain" not in result:
            return None  # a Domain case, and the Domain stage did not run for this variant
        found_ok = case["action"] in found and found[case["action"]]["owner"] is True and found[case["action"]]["due"] is True
        return found_ok and case["action"] not in result.get("domain", {}).get("wrong", {})

    result["known_cases"] = [
        {"id": case["id"], "what": case["what"], "follow_up": case.get("follow_up"), "solved": solved(case)}
        for case in expected.get("known_cases", [])
    ]
    return result


def _score_domain(expected: dict, actions: list[dict], found: dict, typed: list[dict]) -> dict:
    """Action Type and Domain fields of the expected actions that have them; calendar events said to be
    cancelled must not come back as calendar_event."""
    by_id = {t["id"]: t for t in typed}
    checked, wrong = [], {}
    for exp in expected["actions"]:
        if "action_type" not in exp or exp["key"] not in found:
            continue  # an omission is already counted by the generic stage
        checked.append(exp["key"])
        got = by_id.get(f"pa-{found[exp['key']]['index'] + 1}")
        if got is None:
            wrong[exp["key"]] = ["dropped by the Domain stage"]
            continue
        problems = [] if got["action_type"] == exp["action_type"] else [f"action_type {got['action_type']}"]
        for field, accepted in exp.get("fields", {}).items():
            value = (got.get("fields") or {}).get(field)
            if value not in accepted:
                problems.append(f"{field} {value}")
        if problems:
            wrong[exp["key"]] = problems
    events = [t for t in typed if t["action_type"] == "calendar_event"]
    cancelled = [n["key"] for n in expected.get("not_actions", []) if any(_matches(t, n["match"]) for t in events)]
    return {"checked": len(checked), "wrong": wrong, "cancelled_as_event": cancelled}


def totals(per_session: dict[str, dict], names: list[str]) -> dict:
    s = [per_session[n] for n in names]
    expected, produced, found, matched = (sum(r[k] for r in s) for k in ("expected", "produced", "found", "matched"))
    count = lambda k: sum(len(r[k]) for r in s)
    rate = lambda a, b: round(a / b, 3) if b else 0.0
    return {
        "sessions": names,
        "expected": expected, "produced": produced, "found": found,
        "omission_rate": rate(count("omitted"), expected),
        "invented_rate": rate(count("invented"), produced),
        "owner_wrong_rate": rate(count("owner_wrong"), matched),
        "owner_missing": count("owner_missing"),
        "date_wrong_rate": rate(count("date_wrong"), matched),
        "date_missing": count("date_missing"),
        "cancelled_listed": count("cancelled_listed"),
        "numbers": sum(r["numbers"] for r in s),
        "numbers_changed": count("numbers_changed"),
    }


def build_report() -> dict:
    thresholds = _json(HERE / "thresholds.json")
    report: dict = {"thresholds": thresholds, "variants": {}}
    for variant in VARIANTS:
        per_session = {}
        for folder in sessions():
            expected = _json(folder / "expected.json")
            english = _json(folder / "translation.json")["english"]
            actions = _json(folder / f"extract.{variant}.json")["result"].get("proposed_actions", [])
            domain_file = folder / f"domain.{variant}.json"
            typed = _json(domain_file)["result"]["actions"] if domain_file.exists() else None
            per_session[folder.name] = score(expected, actions, " ".join(s["text"] for s in english["segments"]), typed)
        names = list(per_session)
        groups = {
            "corporate_others": [n for n in names if not n.startswith("consult_")],
            "medical": [n for n in names if n.startswith("consult_")],
        }
        summary = {g: totals(per_session, members) for g, members in groups.items()}
        summary["corporate_others"]["meets_thresholds"] = {
            k: summary["corporate_others"][k] <= v for k, v in thresholds["limits"].items()
        }
        domain = [r["domain"] for r in per_session.values() if "domain" in r]
        if domain:
            summary["domain_stage"] = {
                "checked": sum(d["checked"] for d in domain),
                "right": sum(d["checked"] - len(d["wrong"]) for d in domain),
                "wrong": {n: r["domain"]["wrong"] for n, r in per_session.items() if r.get("domain", {}).get("wrong")},
                "cancelled_as_event": sum(len(d["cancelled_as_event"]) for d in domain),
            }
        report["variants"][variant] = {"summary": summary, "sessions": per_session}
    report["variance"] = _variance_summary()
    return report


def _variance_summary() -> dict:
    """Spread of the counts over the recorded run and the extra runs in variance.json (same input each time)."""
    path = HERE / "variance.json"
    if not path.exists():
        return {}
    extra = _json(path)
    out: dict = {"runs": extra["runs"] + 1, "values_seen": {}}
    for variant in VARIANTS:
        for folder in sessions():
            expected = _json(folder / "expected.json")
            text = " ".join(s["text"] for s in _json(folder / "translation.json")["english"]["segments"])
            runs = [_json(folder / f"extract.{variant}.json")["result"].get("proposed_actions", [])]
            runs += extra["actions"][variant].get(folder.name, [])
            scores = [score(expected, actions, text) for actions in runs]
            seen = {k: sorted({len(r[k]) if isinstance(r[k], list) else r[k] for r in scores})
                    for k in ("produced", "omitted", "invented", "owner_wrong", "date_wrong")}
            if any(len(v) > 1 for v in seen.values()):
                out["values_seen"].setdefault(variant, {})[folder.name] = seen
    return out


# ── commands ────────────────────────────────────────────────────────────────────

def record_all() -> int:
    settings = DeepSeekSettings.from_environment()
    for folder in sessions():
        source = source_of(folder)
        client = record.RecordingClient(build_llm_client(settings))
        started = time.perf_counter()
        english = translate(source, client, settings)
        _write(folder / "translation.json", {"model": settings.model, "elapsed_seconds": round(time.perf_counter() - started, 1),
                                             "calls": client.calls, "english": record.english_json(source, english)})
        for variant in VARIANTS:
            client = record.RecordingClient(build_llm_client(settings))
            started = time.perf_counter()
            result = extract(english, variant, source, client, settings)
            _write(folder / f"extract.{variant}.json", {"model": settings.model, "elapsed_seconds": round(time.perf_counter() - started, 1),
                                                        "calls": client.calls, "result": result})
            if variant == "neutral" and source.get("domain") in DOMAIN_STAGE:
                client = record.RecordingClient(build_llm_client(settings))
                started = time.perf_counter()
                typed = domain_stage(english, result, source, client)
                _write(folder / "domain.neutral.json", {"model": settings.model, "elapsed_seconds": round(time.perf_counter() - started, 1),
                                                        "calls": client.calls, "result": typed})
        print(folder.name, "recorded")
    (HERE / "variance.json").unlink(missing_ok=True)  # it was measured on the previous recording
    _write(HERE / "report.json", build_report())
    return 0


def record_domain() -> int:
    """Only the Roles and Domain stage calls, on the recorded translation and neutral extraction:
    for a change to the Domain stage prompt or schema, without re-recording the stages before it."""
    settings = DeepSeekSettings.from_environment()
    for folder in sessions():
        source = source_of(folder)
        if source.get("domain") not in DOMAIN_STAGE:
            continue
        english = record.load_transcript(_json(folder / "translation.json")["english"])
        result = _json(folder / "extract.neutral.json")["result"]
        client = record.RecordingClient(build_llm_client(settings))
        started = time.perf_counter()
        typed = domain_stage(english, result, source, client)
        _write(folder / "domain.neutral.json", {"model": settings.model, "elapsed_seconds": round(time.perf_counter() - started, 1),
                                                "calls": client.calls, "result": typed})
        print(folder.name, "Domain stage recorded")
    _write(HERE / "report.json", build_report())
    return 0


def variance_runs(runs: int) -> int:
    """Extra extract runs on the recorded English Transcripts: temperature 0 still varies between runs."""
    settings = DeepSeekSettings.from_environment()
    actions: dict = {v: {} for v in VARIANTS}
    for folder in sessions():
        source = source_of(folder)
        english = record.load_transcript(_json(folder / "translation.json")["english"])
        for variant in VARIANTS:
            actions[variant][folder.name] = [
                extract(english, variant, source, build_llm_client(settings), settings).get("proposed_actions", [])
                for _ in range(runs)
            ]
        print(folder.name, "done")
    _write(HERE / "variance.json", {"runs": runs, "actions": actions})
    _write(HERE / "report.json", build_report())
    return 0


def check_all(refresh: bool = False) -> int:
    failed = 0
    settings = DeepSeekSettings.from_environment()
    for folder in sessions():
        source = source_of(folder)
        try:
            recorded = _json(folder / "translation.json")
            settings_used = settings.with_overrides(model=recorded["model"])
            client = record.ReplayClient(recorded["calls"])
            english = translate(source, client, settings_used)
            assert not client._calls, "fewer translate calls than recorded"
            assert record.english_json(source, english) == recorded["english"], "translation differs"
            for variant in VARIANTS:
                rec = _json(folder / f"extract.{variant}.json")
                client = record.ReplayClient(rec["calls"])
                result = extract(english, variant, source, client, settings_used)
                assert not client._calls, f"fewer {variant} calls than recorded"
                if refresh:
                    _write(folder / f"extract.{variant}.json", {**rec, "result": result})
                assert result == rec["result"] or refresh, f"{variant} result differs"
                domain_file = folder / f"domain.{variant}.json"
                if domain_file.exists():
                    rec = _json(domain_file)
                    client = record.ReplayClient(rec["calls"])
                    typed = domain_stage(english, result, source, client)  # a changed generic result changes this prompt: record again
                    assert not client._calls, f"fewer {variant} Domain calls than recorded"
                    assert typed == rec["result"], f"{variant} Domain result differs"
            print(f"{folder.name}: OK")
        except AssertionError as exc:
            failed += 1
            print(f"{folder.name}: FAILED: {exc}")
    if refresh:
        _refresh_variance(settings)
        _write(HERE / "report.json", build_report())
    if build_report() != _json(HERE / "report.json"):
        failed += 1
        print("report.json: FAILED: differs from the recordings (run record, or check the scoring change)")
    else:
        print("report.json: OK")
    return 1 if failed else 0


def _refresh_variance(settings) -> None:
    """variance.json holds results, not calls: apply the extractor's own checks again (dates #41, owners #40)."""
    path = HERE / "variance.json"
    if not path.exists():
        return
    from norev.adapters.llm.excerpt_dates import corrected_due_date
    from norev.adapters.llm.owner_check import _checked_action_owner
    data = _json(path)
    for variant, per_session in data["actions"].items():
        for name, runs in per_session.items():
            session_date = date.fromisoformat(source_of(HERE / name)["session_date"])
            turns = record.load_transcript(_json(HERE / name / "translation.json")["english"]).segments
            for actions in runs:
                for a in actions:
                    a["due_date"] = corrected_due_date(a.get("due_date"), a.get("transcript_excerpt"), session_date)
                    a["owner"] = _checked_action_owner(a, turns)
    _write(path, data)


def print_report() -> int:
    report = _json(HERE / "report.json")
    limits = report["thresholds"]["limits"]
    for variant, data in report["variants"].items():
        print(f"== {variant}")
        for group, t in data["summary"].items():
            if group == "domain_stage":
                continue
            print(f"  {group}: found {t['found']}/{t['expected']}, produced {t['produced']}, "
                  f"omissions {t['omission_rate']:.0%}, invented {t['invented_rate']:.0%}, "
                  f"owner wrong {t['owner_wrong_rate']:.0%} (+{t['owner_missing']} missing), "
                  f"date wrong {t['date_wrong_rate']:.0%} (+{t['date_missing']} missing), "
                  f"cancelled listed {t['cancelled_listed']}, numbers changed {t['numbers_changed']}/{t['numbers']}")
        failing = [k for k, ok in data["summary"]["corporate_others"]["meets_thresholds"].items() if not ok]
        print(f"  thresholds ({report['thresholds']['status']}): {'all met' if not failing else 'not met: ' + ', '.join(failing)}")
        d = data["summary"].get("domain_stage")
        if d:
            print(f"  Domain stage (Corporate): {d['right']}/{d['checked']} typed right, calendar events said cancelled kept: {d['cancelled_as_event']}"
                  + "".join(f"\n    {n}: {w}" for n, w in d["wrong"].items()))
        for name, r in data["sessions"].items():
            for case in r["known_cases"]:
                link = f" ({case['follow_up']})" if case.get("follow_up") and not case["solved"] else ""
                state = "not evaluated (no Domain stage)" if case["solved"] is None else "solved" if case["solved"] else "OUTSTANDING"
                print(f"  known case {case['id']}: {state}{link}")
        seen = report.get("variance", {}).get("values_seen", {}).get(variant, {})
        if report.get("variance"):
            runs = report["variance"]["runs"]
            print(f"  between {runs} runs: " + ("; ".join(f"{n} {k} {v}" for n, ks in seen.items() for k, v in ks.items() if len(v) > 1) or "no change"))
    seconds = {"translation": sum(_json(f / "translation.json").get("elapsed_seconds", 0) for f in sessions())}
    for variant in VARIANTS:
        seconds[f"extract {variant}"] = sum(_json(f / f"extract.{variant}.json").get("elapsed_seconds", 0) for f in sessions())
    print("time on the GPU laptop, all Sessions: " + ", ".join(f"{k} {v:.0f}s" for k, v in seconds.items()))
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "record":
        raise SystemExit(record_all())
    if command == "record-domain":
        raise SystemExit(record_domain())
    if command == "variance":
        raise SystemExit(variance_runs(int(sys.argv[2]) if len(sys.argv) > 2 else 2))
    if command in ("check", "refresh"):
        raise SystemExit(check_all(refresh=command == "refresh"))
    raise SystemExit(print_report())

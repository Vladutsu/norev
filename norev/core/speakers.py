"""Speakers on a Transcript, from diarization turns. Pure: no audio, no model (#24)."""
import dataclasses
from collections.abc import Sequence

from norev.core.models import Speaker, SpeakerTurn, Transcript


def assign_speakers(segments: Sequence[tuple[float, float]], turns: Sequence[SpeakerTurn]) -> list[str | None]:
    """Return one speaker_id per (start, end) segment, in the same order.

    The winner is the Speaker with the largest total overlap with the segment (all of that
    Speaker's turns added up). On a tie, the Speaker who starts talking first inside the segment
    wins, then the smaller speaker_id, so the result never depends on the order of `turns`.
    A segment that overlaps no turn gets None: better no Speaker than a guessed one.
    """
    result: list[str | None] = []
    for seg_start, seg_end in segments:
        overlap: dict[str, float] = {}
        first_start: dict[str, float] = {}
        for turn in turns:
            shared = min(seg_end, turn.end) - max(seg_start, turn.start)
            if shared <= 0:
                continue
            overlap[turn.speaker_id] = overlap.get(turn.speaker_id, 0.0) + shared
            first_start[turn.speaker_id] = min(first_start.get(turn.speaker_id, turn.start), max(seg_start, turn.start))
        if not overlap:
            result.append(None)
            continue
        result.append(min(overlap, key=lambda s: (-overlap[s], first_start[s], s)))
    return result


def attach_speakers(transcript: Transcript, turns: Sequence[SpeakerTurn]) -> Transcript:
    """The same Transcript, times and text unchanged, with each Segment's speaker_id set from `turns`.

    Diarizer labels are renamed S1, S2... in order of first appearance, so ids are stable and say
    nothing about who the person is. A Segment no turn overlaps keeps no Speaker.
    """
    speakers = assign_speakers([(s.start, s.end) for s in transcript.segments], turns)
    names: dict[str, str] = {}
    for speaker in speakers:
        if speaker is not None and speaker not in names:
            names[speaker] = f"S{len(names) + 1}"
    segments = tuple(
        dataclasses.replace(segment, speaker_id=names.get(speaker) if speaker is not None else None)
        for segment, speaker in zip(transcript.segments, speakers)
    )
    return dataclasses.replace(transcript, segments=segments)


def speakers_in(transcript: Transcript) -> list[Speaker]:
    """One Speaker per speaker_id of the Transcript, in order of first appearance, with no Role yet."""
    ids: list[str] = []
    for segment in transcript.segments:
        if segment.speaker_id is not None and segment.speaker_id not in ids:
            ids.append(segment.speaker_id)
    return [Speaker(speaker_id) for speaker_id in ids]


def merge_speakers(stored: Sequence[Speaker], new: Sequence[Speaker]) -> list[Speaker]:
    """What a SessionStore saves: `new`, except that a stored role_confirmed Role wins over an
    unconfirmed one for the same Speaker, so the User's correction is never overwritten automatically."""
    confirmed = {s.id: s for s in stored if s.role_confirmed}
    return [s if s.role_confirmed or s.id not in confirmed else confirmed[s.id] for s in new]

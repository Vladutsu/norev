"""Give each Whisper segment the Speaker it overlaps most with. Pure: no audio, no model."""
from collections.abc import Sequence

Turn = tuple[float, float, str]  # (start, end, speaker_id), as returned by a Diarizer


def assign_speakers(segments: Sequence[tuple[float, float]], turns: Sequence[Turn]) -> list[str | None]:
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
        for turn_start, turn_end, speaker_id in turns:
            shared = min(seg_end, turn_end) - max(seg_start, turn_start)
            if shared <= 0:
                continue
            overlap[speaker_id] = overlap.get(speaker_id, 0.0) + shared
            first_start[speaker_id] = min(first_start.get(speaker_id, turn_start), max(seg_start, turn_start))
        if not overlap:
            result.append(None)
            continue
        result.append(min(overlap, key=lambda s: (-overlap[s], first_start[s], s)))
    return result


def split_by_speaker(words: Sequence[tuple[float, float, str]], turns: Sequence[Turn]) -> list[tuple[float, float, str, str]]:
    """Regroup Whisper words (start, end, word) into one (start, end, speaker_id) span per Speaker change.

    A Whisper segment can hold several Speakers, so the Speaker is decided per word. A word that
    overlaps no turn (a gap in the diarization) belongs to the Speaker before it, or after it for
    the first words. Returns spans with the text joined from the words: (start, end, speaker_id, text).
    Without any turn, every word has no Speaker and the result is empty.
    """
    speakers = assign_speakers([(start, end) for start, end, _ in words], turns)
    known = [s for s in speakers if s is not None]
    if not known:
        return []
    filled, last = [], known[0]
    for speaker in speakers:
        last = speaker or last
        filled.append(last)

    spans: list[list] = []
    for (start, end, word), speaker in zip(words, filled):
        if spans and spans[-1][2] == speaker:
            spans[-1][1] = end
            spans[-1][3] += word
        else:
            spans.append([start, end, speaker, word])
    return [(start, end, speaker, " ".join(text.split())) for start, end, speaker, text in spans]

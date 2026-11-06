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

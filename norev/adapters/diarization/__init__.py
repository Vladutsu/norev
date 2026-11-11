

def pick() -> str | None:
    """The installed diarizer: NOREV_DIARIZER (sortformer | sherpa | none) or the first one found.
    Sortformer stops at 4 Speakers; sherpa-onnx clusters any number (a council meeting), so it comes first."""
    import importlib.util
    import os

    wanted = os.getenv("NOREV_DIARIZER", "").strip().lower()
    if wanted == "none":
        return None
    found = [name for name, module in (("sherpa", "sherpa_onnx"), ("sortformer", "nemo"))
             if importlib.util.find_spec(module) is not None]
    if wanted:
        if wanted not in found:
            raise ImportError(f"NOREV_DIARIZER={wanted} is not installed")
        return wanted
    return found[0] if found else None

from pathlib import Path


class DeepSeekPathResolver:
    def output_path_for_input(self, file_path: str, output_dir: Path) -> Path:
        stem = Path(file_path).stem.strip() or "output"
        safe_stem = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in stem)
        deepseek_output_dir = output_dir.parent / "Json"
        return deepseek_output_dir / f"{safe_stem}.proposed_actions.json"

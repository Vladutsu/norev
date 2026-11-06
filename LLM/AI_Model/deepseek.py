from pathlib import Path

from LLM.AI_Model.assigner import TaskAssigner
from LLM.AI_Model.catalog import ModelCatalogService
from LLM.AI_Model.extractor import RealtimeTaskExtractor
from LLM.AI_Model.paths import DeepSeekPathResolver
from LLM.AI_Model.prompt import PM_TASK_PROMPT
from LLM.AI_Model.settings import DeepSeekSettings, LLMSettings

_path_resolver = DeepSeekPathResolver()


def output_path_for_input(file_path: str, output_dir: Path) -> Path:
    return _path_resolver.output_path_for_input(file_path=file_path, output_dir=output_dir)


__all__ = [
    "DeepSeekSettings",
    "LLMSettings",
    "PM_TASK_PROMPT",
    "ModelCatalogService",
    "RealtimeTaskExtractor",
    "TaskAssigner",
    "output_path_for_input",
]

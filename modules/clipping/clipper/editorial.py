"""A small, traceable example bank of actual reference titles.

Examples teach headline wording, never supply facts about the current clip.
The bank is bundled with the application so generation uses the study itself,
not just an abstract summary of it.
"""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def title_examples():
    path = Path(__file__).with_name("reference_titles.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    examples = []
    for item in raw.get("examples", [])[:24]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str)
                                               for k in ("bvid", "title", "technique")):
            continue
        examples.append({k: item[k][:180] for k in ("bvid", "title", "technique")})
    return examples

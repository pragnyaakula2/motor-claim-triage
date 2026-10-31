"""Versioned prompt registry (L2). Prompts are files: prompts/<name>/<version>.txt.

Every response records ``name@version#hash`` so a result can be tied to the exact prompt text.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..config import get_settings


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str
    sha: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}#{self.sha}"


@lru_cache(maxsize=64)
def load_prompt(name: str, version: str, prompts_dir: str | None = None) -> Prompt:
    base = Path(prompts_dir) if prompts_dir else get_settings().prompts_dir
    p = base / name / f"{version}.txt"
    if not p.exists():
        raise FileNotFoundError(f"prompt {name}@{version} not found at {p}")
    text = p.read_text()
    return Prompt(name, version, text, hashlib.sha256(text.encode()).hexdigest()[:8])


def available_versions(name: str) -> list[str]:
    return sorted(p.stem for p in (get_settings().prompts_dir / name).glob("*.txt"))

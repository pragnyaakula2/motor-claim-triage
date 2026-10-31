import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EMBEDDING_BACKEND", "hashing")
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))
os.environ.setdefault("CORPUS_DIR", str(ROOT / "corpus"))


@pytest.fixture(scope="session")
def index_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    os.environ["DATA_DIR"] = str(d)
    from claimtriage.config import reset_settings_cache
    reset_settings_cache()
    from claimtriage.pipeline.build import run
    run()
    return d / "index"


@pytest.fixture(scope="session")
def index(index_dir):
    from claimtriage.retrieval.embedder import get_embedder
    from claimtriage.retrieval.index import PolicyIndex
    return PolicyIndex(index_dir, get_embedder("hashing"))

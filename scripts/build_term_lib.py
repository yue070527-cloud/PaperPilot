"""一次性离线脚本：从 arXiv 论文标题中提取英文术语，生成向量库。

输出文件放在 paperpilot/ 目录下：
    terms_en.npy      — 术语列表 (shape=(N,), dtype=str)
    terms_en_vecs.npy — 向量矩阵 (shape=(N, 384), dtype=float32)

用法:
    python scripts/build_term_lib.py
"""

import os
import sys
import time
from pathlib import Path

import arxiv
import numpy as np
from sklearn.feature_extraction.text import CountVectorizer
from sentence_transformers import SentenceTransformer

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

OUT_TERMS = ROOT / "paperpilot" / "terms_en.npy"
OUT_VECS = ROOT / "paperpilot" / "terms_en_vecs.npy"

BROAD_QUERIES = [
    "physics",
    "chemistry OR materials",
    "biology OR medicine",
    "computer science OR machine learning OR artificial intelligence",
    "engineering OR electronics",
    "mathematics OR statistics",
]
PAPERS_PER_QUERY = 1500
MAX_FEATURES = 5000
NGRAM_RANGE = (1, 3)
BATCH_DELAY = 3.0

_STOP_WORDS_EN = {
    "using", "based", "new", "via", "two", "one", "three",
    "first", "second", "novel", "study", "approach", "towards",
    "case", "part", "without", "within", "also", "can", "may",
    "high", "low", "large", "small",
}


def _fetch_titles() -> list[str]:
    client = arxiv.Client()
    titles = set()
    for query in BROAD_QUERIES:
        print(f"  searching: {query} ...")
        search = arxiv.Search(
            query=query,
            max_results=PAPERS_PER_QUERY,
            sort_by=arxiv.SortCriterion.SubmittedDate,
        )
        for r in client.results(search):
            t = r.title.strip()
            if t:
                titles.add(t)
        print(f"    collected {len(titles)} unique titles so far")
        time.sleep(BATCH_DELAY)
    return list(titles)


def _build_terms(titles: list[str]) -> np.ndarray:
    print(f"  extracting n-grams from {len(titles)} titles ...")
    vec = CountVectorizer(
        ngram_range=NGRAM_RANGE,
        stop_words="english",
        max_features=MAX_FEATURES,
        lowercase=True,
    )
    vec.fit_transform(titles)
    raw = vec.get_feature_names_out()
    filtered = [t for t in raw if t not in _STOP_WORDS_EN and len(t) >= 3]
    print(f"  {len(filtered)} terms after stop-word filter")
    return np.array(filtered, dtype=str)


def _embed_terms(model: SentenceTransformer, terms: np.ndarray) -> np.ndarray:
    print(f"  embedding {len(terms)} terms ...")
    vecs = model.encode(terms.tolist(), normalize_embeddings=True, show_progress_bar=True)
    return vecs.astype(np.float32)


def main():
    print("=" * 50)
    print("Step 1: fetch titles from arXiv")
    titles = _fetch_titles()
    print(f"Total unique titles: {len(titles)}")

    print("\nStep 2: build term vocabulary")
    terms = _build_terms(titles)

    print("\nStep 3: load multilingual model")
    model_path = str(Path.home() / ".cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    if not Path(model_path).exists():
        model_path = "paraphrase-multilingual-MiniLM-L12-v2"
    model = SentenceTransformer(model_path)

    print("\nStep 4: embed terms")
    vecs = _embed_terms(model, terms)

    print(f"\nStep 5: save to {OUT_TERMS} and {OUT_VECS}")
    np.save(OUT_TERMS, terms)
    np.save(OUT_VECS, vecs)
    size_mb = (vecs.nbytes + terms.nbytes) / (1024 * 1024)
    print(f"  terms: {terms.shape}, vecs: {vecs.shape}, total: {size_mb:.1f} MB")
    print("DONE")


if __name__ == "__main__":
    main()

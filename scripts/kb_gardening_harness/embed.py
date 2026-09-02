"""Embed every note once with local MiniLM (proxy for the prod centroid) and cache."""

from __future__ import annotations
import numpy as np
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from corpus import load

CACHE = pathlib.Path(__file__).parent / "emb_cache.npz"


def embeddings(notes=None):
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        return list(z["slugs"]), z["emb"]
    notes = notes or load()
    from sentence_transformers import SentenceTransformer

    m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    slugs = sorted(notes)
    # title + first ~1500 chars of body, like the prod breadcrumb+chunk centroid (roughly)
    texts = [(notes[s]["title"] + "\n" + notes[s]["body"][:1500]) for s in slugs]
    t = time.time()
    emb = m.encode(
        texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False
    )
    np.savez(CACHE, slugs=np.array(slugs), emb=emb)
    print("embedded", len(slugs), "in", round(time.time() - t, 1), "s", file=sys.stderr)
    return slugs, emb


if __name__ == "__main__":
    s, e = embeddings()
    sims = e @ e.T
    np.fill_diagonal(sims, 0)
    for thr in (0.99, 0.97, 0.95, 0.90, 0.85):
        n = int((np.triu(sims, 1) >= thr).sum())
        print(f"pairs >= {thr}: {n}")

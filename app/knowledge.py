"""Tiny BM25 retriever over the clinic's documents (English policies + a Hindi lab-prep sheet).

The agent always queries in English (the system prompt tells Gemini to translate the caller's
Telugu/Hindi question into English search terms), and then answers in the caller's language.
That is the cross-language retrieval path the problem statement asks for, with zero extra infra.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge"
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an the is are to of for and or in on at by be with as it this that from per up".split())


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class KnowledgeBase:
    def __init__(self, folder: Path = KB_DIR):
        self.chunks: list[dict] = []
        for f in sorted(folder.glob("*.md")):
            title = f.stem
            body = f.read_text(encoding="utf-8")
            first = body.splitlines()[0].lstrip("# ").strip() if body else title
            for sec in re.split(r"\n(?=## )", body):
                lines = sec.strip().splitlines()
                if not lines or not lines[0].startswith("## "):
                    continue
                heading = lines[0][3:].strip()
                text = "\n".join(lines[1:]).strip()
                self.chunks.append({"source": f"{first} › {heading}", "file": f.name, "heading": heading,
                                    "text": text, "tokens": _tokens(heading + " " + text + " " + first)})
        self.N = len(self.chunks)
        self.avgdl = sum(len(c["tokens"]) for c in self.chunks) / max(1, self.N)
        df: Counter = Counter()
        for c in self.chunks:
            df.update(set(c["tokens"]))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        q = _tokens(query)
        scored = []
        for c in self.chunks:
            tf = Counter(c["tokens"])
            dl = len(c["tokens"])
            s = 0.0
            for t in q:
                if t in tf:
                    s += self.idf[t] * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * dl / self.avgdl))
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [{"source": c["source"], "text": c["text"], "score": round(s, 2)} for s, c in scored[:top_k]]


KB = KnowledgeBase()


def search_clinic_knowledge(query_en: str, top_k: int = 3) -> dict:
    hits = KB.search(query_en, top_k=max(1, min(int(top_k or 3), 5)))
    if not hits:
        return {"ok": True, "results": [], "note": "Nothing found. Do not guess; offer a staff call-back."}
    return {"ok": True, "results": hits,
            "note": "Answer only from these passages, in the caller's language, and mention the source briefly."}

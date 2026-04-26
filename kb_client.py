"""
KB NetApp HTTP Client
=====================
Features:
  1. Header-aware semantic chunking with 15% context overlap
  2. Hybrid search: BM25 keyword scoring + sentence-transformer cosine similarity
  3. Persistent local article index (embeddings cached in ~/.copilot/kb_index/)
  4. Metadata filtering by domain, team, and valid_after date
  5. Auto-indexing: articles fetched via get_article() are chunked and indexed

Category paths carry (path, domain, team) tuples for metadata-aware filtering.
"""

import hashlib
import json
import logging
import os
import pickle
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests
from bs4 import BeautifulSoup

from auth_manager import get_stored_cookies, get_username

logger = logging.getLogger("kb-mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KB_BASE_URL = "https://kb.netapp.com"

# (path, domain, team) — team used as metadata tag for filtering
KB_CATEGORY_BROWSE_PATHS: List[Tuple[str, str, str]] = [
    ("/on-prem/ontap/Perf/Perf-KBs",               "ONTAP", "Performance"),
    ("/on-prem/ontap/Ontap_OS/OS-KBs",             "ONTAP", "OS"),
    ("/on-prem/ontap/Upgrade/Upgrade-KBs",         "ONTAP", "Upgrade"),
    ("/on-prem/ontap/mc/MC-KBs",                   "ONTAP", "MetroCluster"),
    ("/on-prem/ontap/Mediator/Mediator-KBs",       "ONTAP", "Mediator"),
    ("/on-prem/ontap/OHW/OHW-KBs",                "ONTAP", "Hardware"),
    ("/on-prem/ontap/da/NAS",                      "ONTAP", "NAS"),
    ("/on-prem/ontap/da/SAN",                      "ONTAP", "SAN"),
    ("/on-prem/ontap/da/XCP",                      "ONTAP", "XCP"),
    ("/on-prem/ontap/DP/SnapMirror",               "ONTAP", "SnapMirror"),
    ("/on-prem/ontap/DP/SnapLock",                 "ONTAP", "SnapLock"),
    ("/on-prem/ontap/DP/SnapRestore",              "ONTAP", "SnapRestore"),
    ("/on-prem/ontap/DP/NDMP",                     "ONTAP", "NDMP"),
    ("/on-prem/ontap/DM/FlexGroup",                "ONTAP", "FlexGroup"),
    ("/on-prem/ontap/DM/Encryption",               "ONTAP", "Encryption"),
    ("/on-prem/ontap/DM/Efficiency",               "ONTAP", "Efficiency"),
    ("/on-prem/ontap/DM/FabricPool",               "ONTAP", "FabricPool"),
    ("/on-prem/ontap/DM/System_Manager",           "ONTAP", "SystemManager"),
]

ARTICLE_PATH_MARKERS = [
    "/Perf-KBs/", "/OS-KBs/", "/Upgrade-KBs/", "/MC-KBs/",
    "/Mediator-KBs/", "/OHW-KBs/", "/NAS-KBs/", "/SAN-KBs/",
    "/XCP-KBs/", "/SnapMirror-KBs/", "/SnapLock-KBs/", "/SnapDiff-KBs/",
    "/SnapRestore-KBs/", "/NDMP-KBs/", "/FlexGroup-KBs/", "/Encryption-KBs/",
    "/Efficiency-KBs/", "/FabricPool-KBs/", "/SM-KBs/", "/article/",
]

REQUEST_TIMEOUT = 30
CHUNK_MAX_CHARS = 1200   # ~300 tokens at 4 chars/token
OVERLAP_RATIO   = 0.15   # 15% of previous chunk prepended to next

INDEX_DIR  = Path.home() / ".copilot" / "kb_index"
INDEX_FILE = INDEX_DIR / "article_index.pkl"

_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://kb.netapp.com/",
}

# Injected into every search response — instructs the model to stay grounded
GUARDRAIL_PROMPT = (
    "⚠️  STRICT GUARDRAIL: You MUST answer exclusively from the KB chunks returned "
    "below. Do NOT fabricate procedures, version numbers, error codes, internal "
    "service names, or workarounds that are not explicitly present in the retrieved "
    "content. If the answer cannot be found in the returned chunks, state clearly: "
    "'This information is not available in the retrieved KB articles.'"
)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ArticleChunk:
    chunk_id:       str              # sha256[:12] of chunk text
    article_url:    str
    article_title:  str
    domain:         str              # e.g. "ONTAP"
    team:           str              # e.g. "Performance"
    section_header: str              # heading text, or "" for intro
    text:           str              # chunk text (includes overlap prefix)
    char_start:     int
    char_end:       int
    published_date: Optional[str] = None   # ISO date string
    embedding:      Optional[np.ndarray] = None   # L2-normalized


# ---------------------------------------------------------------------------
# Header-aware chunker with 15% overlap
# ---------------------------------------------------------------------------

# Matches KB article section headings once parsed to plain text:
#   - Short lines (≤100 chars) that are not a full sentence
#   - Start with Title Case or are known KB section labels
#   - Preceded by at least one blank line in the document
_HEADING_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9 /\-:()'\"]{1,79}[^.!?]|#{1,4}\s.+)$"
)


def _split_at_headers(text: str) -> List[Tuple[str, str]]:
    """
    Split document text into (header, body) pairs.
    Heading detection: short lines with Title/ALL-CAPS casing preceded by a blank line.
    Returns at least one section.
    """
    sections: List[Tuple[str, str]] = []
    lines = text.splitlines()

    current_header = ""
    current_body: List[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        prev_blank = (i == 0) or (not lines[i - 1].strip())

        is_heading = (
            stripped
            and len(stripped) <= 100
            and not stripped.endswith((".", ",", ";", "..."))
            and prev_blank
            and _HEADING_RE.match(stripped)
            # Don't treat lone numbers or bullet points as headings
            and not re.match(r"^[\d\-\*\•]", stripped)
        )

        if is_heading:
            # Save accumulated body before starting the new section
            if current_body:
                body = "\n".join(current_body).strip()
                if body:
                    sections.append((current_header, body))
            # Always advance the header (even when current_body was empty,
            # e.g. when the document starts directly with a heading)
            current_header = stripped.lstrip("#").strip()
            current_body = []
        else:
            current_body.append(line)

    # Flush final section
    body = "\n".join(current_body).strip()
    if body:
        sections.append((current_header, body))

    return sections or [("", text)]


def _split_paragraphs(text: str, max_chars: int) -> List[str]:
    """Sub-split oversized sections on blank lines."""
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n{2,}", text)
    result: List[str] = []
    current = ""

    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            result.append(current.strip())
            current = para
        else:
            current = (current + "\n\n" + para) if current else para

    if current.strip():
        result.append(current.strip())

    return result if result else [text]


def chunk_article(
    text: str,
    article_url: str,
    article_title: str,
    domain: str,
    team: str,
    published_date: Optional[str] = None,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> List[ArticleChunk]:
    """
    Split article text into semantic chunks:
      1. Split on detected section headings.
      2. Sub-split oversized sections on paragraph breaks.
      3. Prepend last (overlap_ratio * len(prev_chunk)) chars from previous
         chunk to each new chunk (context window overlap).
    """
    sections = _split_at_headers(text)
    chunks: List[ArticleChunk] = []
    prev_text = ""
    char_offset = 0

    for header, body in sections:
        sub_bodies = _split_paragraphs(body, max_chars)

        for sub in sub_bodies:
            # Build overlap prefix from the tail of the previous raw chunk
            overlap_chars = int(len(prev_text) * overlap_ratio)
            overlap_prefix = prev_text[-overlap_chars:].strip() if overlap_chars and prev_text else ""

            if overlap_prefix:
                full_text = f"[…continued]\n{overlap_prefix}\n\n{sub}"
            else:
                full_text = sub

            if header:
                full_text = f"## {header}\n{full_text}"

            chunk_id = hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:12]
            chunks.append(ArticleChunk(
                chunk_id=chunk_id,
                article_url=article_url,
                article_title=article_title,
                domain=domain,
                team=team,
                section_header=header,
                text=full_text,
                char_start=char_offset,
                char_end=char_offset + len(body),
                published_date=published_date,
            ))
            prev_text = sub
            char_offset += len(body) + 1

    return chunks


# ---------------------------------------------------------------------------
# Embedding engine (lazy-loaded singleton, requires sentence-transformers)
# ---------------------------------------------------------------------------

class EmbeddingEngine:
    """
    Wraps sentence-transformers/all-MiniLM-L6-v2.
    Falls back gracefully if the library is not installed.
    All returned vectors are L2-normalised (dot product == cosine similarity).
    """

    _instance: Optional["EmbeddingEngine"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._available: Optional[bool] = None

    @classmethod
    def get(cls) -> "EmbeddingEngine":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_available(self) -> bool:
        if self._available is None:
            try:
                import sentence_transformers  # noqa: F401
                self._available = True
            except ImportError:
                self._available = False
                logger.warning(
                    "sentence-transformers not installed. "
                    "Semantic search disabled — BM25 only. "
                    "Run: pip install sentence-transformers"
                )
        return self._available

    def _load(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            # Support custom CA bundle for corporate environments with SSL inspection
            # (e.g. Zscaler). Set KB_CA_BUNDLE in mcp-config.json env.
            ca = os.environ.get("KB_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")
            if ca:
                os.environ["REQUESTS_CA_BUNDLE"] = ca
                os.environ["CURL_CA_BUNDLE"] = ca
                os.environ["SSL_CERT_FILE"] = ca
                # Also patch huggingface_hub's httpx client (used for model downloads)
                try:
                    import httpx
                    from huggingface_hub.utils._http import set_client_factory
                    _ca = ca
                    def _hf_client_factory() -> httpx.Client:
                        return httpx.Client(verify=_ca)
                    set_client_factory(_hf_client_factory)
                except Exception:
                    pass
            # Use local_files_only=True to skip HuggingFace Hub version-check network
            # request, which can timeout (≈45s) through corporate SSL inspection (Zscaler).
            # Falls back to online download if not locally cached yet.
            try:
                self._model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
            except Exception:
                self._model = SentenceTransformer("all-MiniLM-L6-v2")

    def encode(self, texts: List[str]) -> Optional[np.ndarray]:
        """Return (N, D) float32 array of L2-normalised embeddings, or None."""
        if not self.is_available():
            return None
        try:
            self._load()
            return self._model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False, batch_size=32
            )
        except Exception as exc:
            # Model download blocked (e.g. corporate SSL) or inference error.
            # Degrade gracefully to BM25-only mode.
            logger.warning(
                "Embedding model unavailable (%s). Falling back to BM25-only search.", exc
            )
            self._available = False
            return None

    def encode_query(self, query: str) -> Optional[np.ndarray]:
        result = self.encode([query])
        return result[0] if result is not None else None


# ---------------------------------------------------------------------------
# Reranking engine (lazy-loaded singleton)
# ---------------------------------------------------------------------------

RERANK_POOL_FACTOR = 3   # collect this many × limit candidates before reranking


class RerankEngine:
    """
    Wraps cross-encoder/ms-marco-MiniLM-L-6-v2 for post-retrieval reranking.
    Falls back gracefully if the model cannot be loaded.

    Usage: called after hybrid retrieval on the top-N candidate pool to produce
    a more accurate final ranking before returning results to the caller.
    """

    _instance: Optional["RerankEngine"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._available: Optional[bool] = None

    @classmethod
    def get(cls) -> "RerankEngine":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_available(self) -> bool:
        if self._available is None:
            try:
                from sentence_transformers.cross_encoder import CrossEncoder  # noqa: F401
                self._available = True
            except ImportError:
                self._available = False
                logger.warning(
                    "CrossEncoder not available — reranking disabled. "
                    "Ensure sentence-transformers>=2.7.0 is installed."
                )
        return self._available

    def _load(self) -> None:
        if self._model is None:
            from sentence_transformers.cross_encoder import CrossEncoder
            ca = os.environ.get("KB_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")
            if ca:
                os.environ["REQUESTS_CA_BUNDLE"] = ca
                os.environ["CURL_CA_BUNDLE"] = ca
                os.environ["SSL_CERT_FILE"] = ca
                try:
                    import httpx
                    from huggingface_hub.utils._http import set_client_factory
                    _ca = ca
                    def _hf_client_factory() -> httpx.Client:
                        return httpx.Client(verify=_ca)
                    set_client_factory(_hf_client_factory)
                except Exception:
                    pass
            # Prefer local pre-downloaded copy (avoids corporate SSL issues on download)
            _local = os.path.expanduser(
                "~/.cache/huggingface/models/cross-encoder-ms-marco-MiniLM-L-6-v2"
            )
            model_name = _local if os.path.isdir(_local) else "cross-encoder/ms-marco-MiniLM-L-6-v2"
            self._model = CrossEncoder(model_name)

    def rerank(self, query: str, results: List[dict]) -> List[dict]:
        """
        Rerank result dicts by (query, title + snippet) cross-encoder score.
        Adds 'rerank_score' to each result. Returns sorted descending by score.
        No-ops and returns original order if unavailable or on error.
        """
        if not results or not self.is_available():
            return results
        try:
            self._load()
            pairs = [
                (query, f"{r.get('title', '')}\n{r.get('snippet', '')}"[:512])
                for r in results
            ]
            scores = self._model.predict(pairs)
            for r, score in zip(results, scores):
                r["rerank_score"] = round(float(score), 4)
            return sorted(results, key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        except Exception as exc:
            logger.warning("Reranking failed (%s) — returning original order.", exc)
            return results


# ---------------------------------------------------------------------------
# Persistent article index
# ---------------------------------------------------------------------------

class ArticleIndex:
    """
    In-memory + on-disk store for ArticleChunk objects with embeddings.
    Serialised to ~/.copilot/kb_index/article_index.pkl via pickle.
    Thread-safe for concurrent reads and single-writer updates.
    """

    def __init__(self) -> None:
        self._chunks: Dict[str, ArticleChunk] = {}       # chunk_id → chunk
        self._url_chunks: Dict[str, List[str]] = {}      # article_url → [chunk_ids]
        self._lock = threading.Lock()
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if INDEX_FILE.exists():
            try:
                with open(INDEX_FILE, "rb") as fh:
                    data = pickle.load(fh)
                self._chunks = data.get("chunks", {})
                self._url_chunks = data.get("url_chunks", {})
                logger.info("Loaded KB index: %d chunks from %d articles",
                            len(self._chunks), len(self._url_chunks))
            except Exception as exc:
                logger.warning("Could not load KB index (%s) — starting fresh.", exc)
                self._chunks = {}
                self._url_chunks = {}

    def save(self) -> None:
        try:
            with open(INDEX_FILE, "wb") as fh:
                pickle.dump({
                    "chunks": self._chunks,
                    "url_chunks": self._url_chunks,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }, fh)
        except Exception as exc:
            logger.warning("Could not save KB index: %s", exc)

    def add_chunks(self, chunks: List[ArticleChunk]) -> None:
        with self._lock:
            for chunk in chunks:
                self._chunks[chunk.chunk_id] = chunk
            if chunks:
                url = chunks[0].article_url
                self._url_chunks[url] = [c.chunk_id for c in chunks]

    def has_article(self, url: str) -> bool:
        return url in self._url_chunks

    def get_filtered_chunks(
        self,
        domain: Optional[str] = None,
        team: Optional[str] = None,
        valid_after: Optional[str] = None,
    ) -> List[ArticleChunk]:
        result = []
        for chunk in self._chunks.values():
            if domain and chunk.domain.lower() != domain.lower():
                continue
            if team and chunk.team.lower() != team.lower():
                continue
            if valid_after and chunk.published_date:
                try:
                    if chunk.published_date < valid_after:
                        continue
                except Exception:
                    pass
            result.append(chunk)
        return result

    @property
    def total_chunks(self) -> int:
        return len(self._chunks)

    @property
    def total_articles(self) -> int:
        return len(self._url_chunks)


# Module-level singleton
_index: Optional[ArticleIndex] = None
_index_lock = threading.Lock()


def get_index() -> ArticleIndex:
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = ArticleIndex()
    return _index


# ---------------------------------------------------------------------------
# BM25 scoring with rank-bm25 fallback
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercase alpha-numeric tokenisation (no stopword removal for simplicity)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25_scores(query_tokens: List[str], corpus_tokens: List[List[str]]) -> np.ndarray:
    """
    BM25 Okapi scores (requires rank-bm25).
    Falls back to simple term-frequency overlap if library unavailable.
    """
    if not corpus_tokens:
        return np.array([], dtype=float)
    try:
        from rank_bm25 import BM25Okapi
        scores = BM25Okapi(corpus_tokens).get_scores(query_tokens)
        return np.asarray(scores, dtype=float)
    except ImportError:
        # TF-overlap fallback
        query_set = set(query_tokens)
        scores = np.array(
            [sum(1 for t in doc if t in query_set) for doc in corpus_tokens],
            dtype=float,
        )
        return scores


def _normalize(arr: np.ndarray) -> np.ndarray:
    """Min-max normalise to [0, 1], avoiding division by zero."""
    span = arr.max() - arr.min()
    return (arr - arr.min()) / span if span > 0 else np.zeros_like(arr)


# ---------------------------------------------------------------------------
# HTTP session builder
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    username = get_username()
    cookie_data = get_stored_cookies(username)
    jar = requests.cookies.RequestsCookieJar()
    for c in cookie_data.get("cookies", []):
        jar.set(c["name"], c["value"],
                domain=c.get("domain", ".netapp.com"),
                path=c.get("path", "/"))
    session = requests.Session()
    session.cookies = jar
    session.headers.update(_COMMON_HEADERS)
    return session


def _check_auth(resp: requests.Response) -> Optional[dict]:
    if resp.status_code in (401, 403):
        return {"error": f"Auth failed (HTTP {resp.status_code}). Run Set-KBCookies.ps1."}
    return None


# ---------------------------------------------------------------------------
# Category page crawler  (with 10-minute in-process cache)
# ---------------------------------------------------------------------------

_browse_cache: Dict[str, Tuple] = {}   # key -> (timestamp, results)
_BROWSE_CACHE_TTL = 600                # seconds

def _browse_categories(
    session: requests.Session,
    domain_filter: Optional[str] = None,
    team_filter: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Browse KB category listing pages and return article link candidates.
    Each result: {title, url, domain, team}.
    Results are cached in-process for 10 minutes to avoid repeated HTTP hits.
    """
    import time as _time
    cache_key = f"{domain_filter}|{team_filter}"
    cached = _browse_cache.get(cache_key)
    if cached:
        ts, data = cached
        if _time.monotonic() - ts < _BROWSE_CACHE_TTL:
            return data

    candidates: List[Dict[str, str]] = []
    seen: set = set()

    for path, domain, team in KB_CATEGORY_BROWSE_PATHS:
        if domain_filter and domain.lower() != domain_filter.lower():
            continue
        if team_filter and team.lower() != team_filter.lower():
            continue
        try:
            resp = session.get(f"{KB_BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href, title = a["href"], a.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                if not any(m in href for m in ARTICLE_PATH_MARKERS):
                    continue
                if not href.startswith("http"):
                    href = KB_BASE_URL + href
                if href in seen:
                    continue
                seen.add(href)
                candidates.append({"title": title, "url": href,
                                   "domain": domain, "team": team})
        except Exception:
            continue

    import time as _time
    _browse_cache[cache_key] = (_time.monotonic(), candidates)
    return candidates


# ---------------------------------------------------------------------------
# Public API — semantic_search
# ---------------------------------------------------------------------------

def semantic_search(
    query: str,
    domain: Optional[str] = None,
    team: Optional[str] = None,
    valid_after: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """
    Hybrid BM25 + semantic vector search over KB articles.

    Scoring:  score = 0.4 × BM25_norm + 0.6 × cosine_sim
    If sentence-transformers unavailable: score = BM25_norm only.

    Pipeline:
      1. Load indexed chunks matching metadata filters.
      2. Browse category pages for fresh (un-indexed) article titles.
      3. Embed query + all candidate texts (or use cached embeddings).
      4. Compute hybrid scores, deduplicate by article URL, return top-K.
    """
    index = get_index()
    engine = EmbeddingEngine.get()
    session = _build_session()

    query_tokens = _tokenize(query)
    query_vec = engine.encode_query(query)   # (D,) or None

    # --- Collect indexed chunks (full text available) ---
    indexed_chunks = index.get_filtered_chunks(domain=domain, team=team, valid_after=valid_after)

    # --- Collect fresh article titles from category pages ---
    # Skip browsing if the index is already well-populated (>100 chunks for this filter),
    # to avoid the HTTP overhead on every search call.
    fresh_chunks: List[ArticleChunk] = []
    if len(indexed_chunks) < 100:
        fresh_candidates = _browse_categories(session, domain_filter=domain, team_filter=team)
        for cand in fresh_candidates:
            if not index.has_article(cand["url"]):
                cid = hashlib.sha256(cand["title"].encode()).hexdigest()[:12]
                fresh_chunks.append(ArticleChunk(
                    chunk_id=cid,
                    article_url=cand["url"],
                    article_title=cand["title"],
                    domain=cand["domain"],
                    team=cand["team"],
                    section_header="",
                    text=cand["title"],
                    char_start=0,
                    char_end=len(cand["title"]),
                ))

    all_chunks = indexed_chunks + fresh_chunks
    if not all_chunks:
        return {
            "query": query,
            "filters": {"domain": domain, "team": team, "valid_after": valid_after},
            "result_count": 0,
            "results": [],
            "guardrail": GUARDRAIL_PROMPT,
            "note": "No KB articles found. Check VPN and authentication.",
        }

    texts = [c.text for c in all_chunks]

    # --- BM25 ---
    corpus_tokens = [_tokenize(t) for t in texts]
    bm25_norm = _normalize(_bm25_scores(query_tokens, corpus_tokens))

    # --- Semantic ---
    if query_vec is not None:
        # Gather embeddings: use cached where available, batch-encode the rest
        embeddings: List[Optional[np.ndarray]] = [c.embedding for c in all_chunks]
        missing_idx = [i for i, e in enumerate(embeddings) if e is None]

        if missing_idx:
            batch = engine.encode([texts[i] for i in missing_idx])
            if batch is not None:
                for pos, i in enumerate(missing_idx):
                    embeddings[i] = batch[pos]

        # Build matrix — fall back to BM25-only for any still-missing embeddings
        valid = [i for i, e in enumerate(embeddings) if e is not None]
        if valid:
            matrix = np.vstack([embeddings[i] for i in valid])   # (M, D)
            cos_sims = matrix @ query_vec                         # (M,)
            # Fill full array
            full_cos = np.zeros(len(all_chunks), dtype=float)
            for pos, i in enumerate(valid):
                full_cos[i] = float(cos_sims[pos])
            hybrid = 0.4 * bm25_norm + 0.6 * full_cos
        else:
            hybrid = bm25_norm
    else:
        hybrid = bm25_norm

    # --- Rank and deduplicate by article URL ---
    # Collect a larger pool first so the reranker has enough candidates to work with.
    reranker = RerankEngine.get()
    rerank_pool = min(limit * RERANK_POOL_FACTOR, 25) if reranker.is_available() else limit

    ranked = np.argsort(hybrid)[::-1]
    seen_urls: set = set()
    results = []

    for i in ranked:
        chunk = all_chunks[i]
        if chunk.article_url in seen_urls:
            continue
        seen_urls.add(chunk.article_url)
        results.append({
            "title":          chunk.article_title,
            "url":            chunk.article_url,
            "domain":         chunk.domain,
            "team":           chunk.team,
            "section":        chunk.section_header or "(intro)",
            "snippet":        chunk.text[:500],
            "score":          round(float(hybrid[i]), 4),
            "published_date": chunk.published_date,
        })
        if len(results) >= rerank_pool:
            break

    # --- Rerank and trim to requested limit ---
    results = reranker.rerank(query, results)
    results = results[:limit]

    return {
        "query":          query,
        "filters":        {"domain": domain, "team": team, "valid_after": valid_after},
        "result_count":   len(results),
        "index_stats":    {"chunks": index.total_chunks, "articles": index.total_articles},
        "results":        results,
        "guardrail":      GUARDRAIL_PROMPT,
        "note": (
            "Scored with BM25 + semantic similarity, reranked with MiniLM cross-encoder. "
            "Use kb_get_article to fetch full content and auto-index for richer future results."
        ),
    }


# ---------------------------------------------------------------------------
# Public API — keyword_lookup
# ---------------------------------------------------------------------------

def keyword_lookup(
    term: str,
    domain: Optional[str] = None,
    team: Optional[str] = None,
    valid_after: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """
    Exact case-insensitive keyword search for error codes, internal terms,
    command names, or service names.

    Searches:
      1. Indexed article chunks (full-text exact match + context excerpt).
      2. Category page article titles (live crawl for un-indexed articles).
    """
    index = get_index()
    session = _build_session()
    term_lower = term.lower()

    # --- Search indexed chunks ---
    indexed_hits: Dict[str, dict] = {}
    for chunk in index.get_filtered_chunks(domain=domain, team=team, valid_after=valid_after):
        text_lower = chunk.text.lower()
        if term_lower not in text_lower:
            continue
        url = chunk.article_url
        if url not in indexed_hits:
            indexed_hits[url] = {
                "title":          chunk.article_title,
                "url":            url,
                "domain":         chunk.domain,
                "team":           chunk.team,
                "published_date": chunk.published_date,
                "matching_excerpts": [],
            }
        # Build context excerpt centred on the first match
        pos = text_lower.find(term_lower)
        start = max(0, pos - 100)
        end   = min(len(chunk.text), pos + len(term) + 100)
        excerpt = f"…{chunk.text[start:end]}…"
        indexed_hits[url]["matching_excerpts"].append({
            "section": chunk.section_header or "(intro)",
            "excerpt": excerpt,
        })

    # --- Live title scan for un-indexed articles ---
    fresh_hits: Dict[str, dict] = {}
    for cand in _browse_categories(session, domain_filter=domain, team_filter=team):
        if cand["url"] in indexed_hits:
            continue
        if term_lower in cand["title"].lower():
            fresh_hits[cand["url"]] = {
                "title":             cand["title"],
                "url":               cand["url"],
                "domain":            cand["domain"],
                "team":              cand["team"],
                "published_date":    None,
                "matching_excerpts": [],
            }

    all_results = list(indexed_hits.values()) + list(fresh_hits.values())
    all_results = all_results[:limit]

    return {
        "term":         term,
        "filters":      {"domain": domain, "team": team, "valid_after": valid_after},
        "result_count": len(all_results),
        "results":      all_results,
        "guardrail":    GUARDRAIL_PROMPT,
    }


# ---------------------------------------------------------------------------
# Public API — get_article  (fetches + auto-indexes)
# ---------------------------------------------------------------------------

def get_article(
    article_id_or_url: str,
    domain: str = "ONTAP",
    team: str = "General",
) -> dict:
    """
    Fetch a KB article by ID, path, or full URL.
    Auto-chunks and indexes the content for future hybrid searches.
    """
    session = _build_session()
    url = _resolve_article_url(article_id_or_url)

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        auth_err = _check_auth(resp)
        if auth_err:
            return auth_err
        resp.raise_for_status()

        if "json" in resp.headers.get("content-type", ""):
            return resp.json()

        result = _parse_article_html(resp.text, url)

        # Auto-index if we got content
        content = result.get("content", "")
        title   = result.get("title", "")
        if content and title and not get_index().has_article(url):
            _index_article(
                content=content,
                url=url,
                title=title,
                domain=domain,
                team=team,
                published_date=result.get("metadata", {}).get("date"),
            )

        return result

    except requests.RequestException as exc:
        return {"error": str(exc)}


def _index_article(
    content: str,
    url: str,
    title: str,
    domain: str,
    team: str,
    published_date: Optional[str] = None,
) -> None:
    """Chunk, embed, and persist an article."""
    index  = get_index()
    engine = EmbeddingEngine.get()

    chunks = chunk_article(
        text=content, article_url=url, article_title=title,
        domain=domain, team=team, published_date=published_date,
    )
    if not chunks:
        return

    if engine.is_available():
        embeddings = engine.encode([c.text for c in chunks])
        if embeddings is not None:
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb

    index.add_chunks(chunks)
    index.save()
    logger.info("Indexed %d chunks for: %s", len(chunks), title)


# ---------------------------------------------------------------------------
# Public API — fetch_kb_url  (pass-through, same as v1)
# ---------------------------------------------------------------------------

def fetch_kb_url(url: str) -> dict:
    """Fetch any kb.netapp.com or netapp.com URL and return parsed content."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.netloc.endswith("netapp.com"):
        return {"error": f"Only netapp.com URLs allowed. Got: {url}"}
    if not parsed.scheme:
        url = "https://" + url

    session = _build_session()
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        auth_err = _check_auth(resp)
        if auth_err:
            return auth_err
        resp.raise_for_status()
        if "json" in resp.headers.get("content-type", ""):
            return resp.json()
        return _parse_article_html(resp.text, url)
    except requests.RequestException as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# HTML parser (enhanced from v1 — also extracts date metadata)
# ---------------------------------------------------------------------------

def _resolve_article_url(id_or_url: str) -> str:
    from urllib.parse import urljoin
    s = id_or_url.strip()
    if s.startswith("http"):
        return s
    if re.match(r"^\d+$", s):
        return f"{KB_BASE_URL}/on-prem/ontap/article/{s}"
    return urljoin(KB_BASE_URL + "/", s.lstrip("/"))


def _parse_article_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title = ""
    for sel in ["h1", "title"]:
        tag = soup.select_one(sel)
        if tag:
            title = tag.get_text(strip=True)
            if title and title != "NetApp":
                break

    # Metadata (now also captures 'date' for valid_after filtering)
    metadata: dict = {}
    for meta in soup.select("meta[name]"):
        name = meta.get("name", "")
        val  = meta.get("content", "")
        if name and val and name in ("description", "keywords", "product", "version", "date"):
            metadata[name] = val

    # Article body
    content_text = ""
    for sel in [
        "div.mt-content-container",
        "article.elm-content-container",
        "div.elm-content-container",
        "main",
        "article",
    ]:
        tag = soup.select_one(sel)
        if tag:
            for noise in tag.select(
                "nav, .mt-social-share, .mt-page-stats, "
                ".mt-translate-container, script, style, "
                ".elm-related-articles-container"
            ):
                noise.decompose()
            text = tag.get_text(separator="\n", strip=True)
            if len(text) > len(content_text):
                content_text = text

    # Related article links
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_title = a.get_text(strip=True)
        if link_title and len(link_title) > 8 and any(m in href for m in ARTICLE_PATH_MARKERS):
            if not href.startswith("http"):
                href = KB_BASE_URL + href
            links.append({"title": link_title, "url": href})

    content_text = re.sub(r"\n{3,}", "\n\n", content_text).strip()

    result: dict = {
        "url":       url,
        "title":     title,
        "metadata":  metadata,
        "content":   content_text[:15000],
        "truncated": len(content_text) > 15000,
    }
    if links:
        result["article_links"] = links
    return result

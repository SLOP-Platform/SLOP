"""backend/core/rag.py

Local RAG (Retrieval-Augmented Generation) knowledge base for the health agent.

Uses a simple TF-IDF based retrieval when chromadb is not available,
upgrading transparently to chromadb vector search when it is installed.

The knowledge base contains:
  - Common Docker/container error patterns and their fixes
  - Traefik configuration reference snippets
  - App-specific troubleshooting (Sonarr, Radarr, Plex etc.)
  - The user's own config context (added at index time)

Usage:
  retriever = get_retriever()
  chunks = retriever.query("database is locked sonarr error", n=3)
  # Returns list of relevant text chunks to inject into the LLM prompt
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Knowledge base documents
# ---------------------------------------------------------------------------

KNOWLEDGE_BASE: list[dict[str, str]] = [
    # Docker errors
    {
        "id": "docker_oom",
        "title": "Container killed: OOM",
        "text": "Container killed with exit code 137 means Out of Memory. "
        "The container exceeded its memory limit. Fix: increase container memory limit, "
        "reduce the number of running containers, or add more RAM to the host. "
        "Check: docker stats --no-stream",
    },
    {
        "id": "docker_permission",
        "title": "Permission denied in container",
        "text": "Permission denied errors in containers usually mean PUID/PGID mismatch. "
        "The container user (PUID/PGID) doesn't own the mounted files. "
        "Fix: chown the host directory to match PUID:PGID, or check /etc/passwd in the container. "
        "For LinuxServer images, set PUID and PGID environment variables.",
    },
    {
        "id": "docker_port_conflict",
        "title": "Port already in use",
        "text": "Error 'address already in use' or 'port is already allocated' means another process "
        "is using the same port. Fix: check what's using the port with 'ss -tlnp | grep PORT', "
        "stop the conflicting service, or change the host port mapping.",
    },
    {
        "id": "docker_network",
        "title": "Container not on network",
        "text": "Container can't reach other containers: check they're on the same Docker network. "
        "All SLOP apps should be on the 'slop' network. "
        "Fix: docker network inspect slop — verify the container appears in Containers list.",
    },
    # Database errors
    {
        "id": "db_locked",
        "title": "SQLite database is locked",
        "text": "Database locked error means two processes are trying to write simultaneously, "
        "or a previous process crashed without releasing the lock. "
        "Fix: restart the container. If it persists, check for zombie processes, "
        "or delete the .db-journal file in the config directory.",
    },
    {
        "id": "db_corrupt",
        "title": "Database corruption",
        "text": "Database corruption usually happens from sudden power loss or disk full. "
        "Fix: sqlite3 database.db 'PRAGMA integrity_check'. "
        "For arr apps, restore from backup in /config/Backups/ or /config/backups/. "
        "Most arr apps make automatic backups.",
    },
    # Traefik errors
    {
        "id": "traefik_cert",
        "title": "Traefik certificate not issued",
        "text": "Certificate not being issued: check CF_DNS_API_TOKEN is set and valid. "
        "Verify Cloudflare API token has 'Zone:DNS:Edit' permission. "
        "Check traefik logs: docker logs traefik | grep -i acme. "
        "Let's Encrypt rate limit: 5 certs per week per domain — use staging resolver for testing. "
        "DNS propagation can take up to 2 minutes — Traefik waits 30s by default.",
    },
    {
        "id": "traefik_middleware",
        "title": "Traefik middleware not found",
        "text": "Error 'middleware X@docker does not exist': the middleware is defined via labels "
        "on a container that isn't running yet. This is non-fatal for Traefik itself. "
        "For traefik-auth@docker: deploy TinyAuth infra slot first. "
        "Traefik will pick it up automatically once the container starts.",
    },
    {
        "id": "traefik_docker_api",
        "title": "Traefik Docker API version mismatch",
        "text": "Error 'client version 1.24 is too old': Traefik v3.3 has a known incompatibility "
        "with Docker 29+. Fix: use traefik:v3.2 or traefik:latest image tag. "
        "Update the compose fragment and recreate the container.",
    },
    # Sonarr/Radarr
    {
        "id": "arr_indexer",
        "title": "Sonarr/Radarr indexer errors",
        "text": "No indexers configured: install Prowlarr first, then sync it to Sonarr/Radarr "
        "via Settings > Indexers > Prowlarr. "
        "Indexer returning errors: check the indexer is accessible and not rate-limited. "
        "Test individual indexers in Prowlarr > Indexers > Test.",
    },
    {
        "id": "arr_download",
        "title": "Sonarr/Radarr download client errors",
        "text": "Download client not configured or unreachable: configure qBittorrent or SABnzbd "
        "in Settings > Download Clients. "
        "If using Gluetun VPN: make sure the download client container uses network_mode: "
        "service:gluetun and is on the correct network.",
    },
    {
        "id": "arr_quality",
        "title": "No downloads found by arr apps",
        "text": "Sonarr/Radarr not downloading: check quality profiles allow the available formats. "
        "Check indexers are working (Prowlarr > Indexers > Test All). "
        "Check release restrictions aren't too strict. "
        "Manual search in the app often reveals why auto-search fails.",
    },
    # Plex/Jellyfin
    {
        "id": "media_transcode",
        "title": "Plex/Jellyfin transcoding issues",
        "text": "Transcoding failing or slow: check hardware transcoding is enabled if available. "
        "For Plex: Settings > Transcoder > Use hardware acceleration. "
        "For Jellyfin: Admin > Playback > Hardware Acceleration. "
        "Ensure the host GPU driver is accessible inside the container "
        "via device mappings (/dev/dri for Intel/AMD, nvidia runtime for NVIDIA).",
    },
    {
        "id": "plex_claim",
        "title": "Plex not signing in",
        "text": "Plex claim token expired or missing: get a fresh token from plex.tv/claim "
        "and add it as PLEX_CLAIM environment variable. Tokens expire in 4 minutes. "
        "For existing servers, sign in via the Plex web UI directly.",
    },
    # Storage/disk
    {
        "id": "disk_full",
        "title": "Disk full",
        "text": "No space left on device: check disk usage with 'df -h'. "
        "Docker images accumulate — clean with 'docker system prune -a'. "
        "Check Docker volumes: 'docker system df'. "
        "Large log files: truncate with 'truncate -s 0 /var/lib/docker/containers/*/logs/*.log'.",
    },
    # General container
    {
        "id": "container_restart",
        "title": "Container restart loop",
        "text": "Container in restart loop (status: restarting): check logs immediately after restart "
        "with 'docker logs CONTAINER --tail 50'. "
        "Common causes: missing config file, bad environment variable, port conflict, "
        "insufficient permissions on mounted directories, missing dependency container.",
    },
    {
        "id": "container_unhealthy",
        "title": "Container health check failing",
        "text": "Container shows as 'unhealthy': the health check command is failing. "
        "Check the actual health check: docker inspect CONTAINER | grep -A5 Health. "
        "Common: web service not responding to HTTP health check, "
        "database not accepting connections, wrong port configured.",
    },
]


# ---------------------------------------------------------------------------
# Simple TF-IDF retriever (no dependencies)
# ---------------------------------------------------------------------------


@dataclass
class SimpleRetriever:
    """Lightweight TF-IDF retriever. No external dependencies."""

    docs: list[dict[str, str]] = field(default_factory=list)
    _idf: dict[str, float] = field(default_factory=dict)
    _tfs: list[dict[str, float]] = field(default_factory=list)

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"\b[a-z]{2,}\b", text.lower())

    def build(self, docs: list[dict[str, str]]) -> None:
        self.docs = docs
        N = len(docs)
        df: dict[str, int] = {}
        all_tfs: list[dict[str, float]] = []

        for doc in docs:
            tokens = self._tokenize(doc["title"] + " " + doc["text"])
            tf: dict[str, float] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            # Normalize TF
            total = sum(tf.values()) or 1
            tf = {k: v / total for k, v in tf.items()}
            all_tfs.append(tf)
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1

        self._idf = {t: math.log(N / (1 + n)) for t, n in df.items()}
        self._tfs = all_tfs

    def query(self, text: str, n: int = 3) -> list[str]:
        """Return top-n relevant document texts."""
        if not self.docs:
            return []
        q_tokens = self._tokenize(text)
        scores: list[float] = []
        for tf in self._tfs:
            score = sum(tf.get(t, 0) * self._idf.get(t, 0) for t in q_tokens)
            scores.append(score)
        top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n]
        return [f"[{self.docs[i]['title']}]\n{self.docs[i]['text']}" for i in top if scores[i] > 0]


# ---------------------------------------------------------------------------
# ChromaDB retriever (optional — better semantic search)
# ---------------------------------------------------------------------------


class ChromaRetriever:
    """Vector-search retriever using chromadb + sentence-transformers."""

    # Persistent guard: the set of IDs from the static KNOWLEDGE_BASE (defined
    # at module load time). Only documents whose IDs appear here may be indexed
    # into chromadb — agent-derived entries (e.g. from cloud_llm escalation
    # appended to KNOWLEDGE_BASE at runtime) are silently excluded.
    #
    # Security rationale (id=729): cloud_llm._index_escalation_in_rag appends
    # agent-generated text to KNOWLEDGE_BASE in-memory and then force-triggers a
    # RAG rebuild. Without this guard those agent-derived strings would be
    # persisted to the on-disk chromadb collection and returned to future LLM
    # prompts, creating a vector-store injection path. This guard ensures only
    # the static, operator-reviewed KB entries reach persistent storage.
    _STATIC_KB_IDS: frozenset[str] = frozenset(d["id"] for d in KNOWLEDGE_BASE)

    def __init__(self, persist_dir: Path) -> None:
        self._dir = persist_dir
        self._collection: Any = None
        self._ready = False

    def build(self, docs: list[dict[str, str]]) -> None:
        """Index static KNOWLEDGE_BASE documents into chromadb.

        Only documents whose IDs are in ``_STATIC_KB_IDS`` (the IDs present at
        module-load time) are persisted. Agent-derived documents appended to
        KNOWLEDGE_BASE at runtime are rejected before they reach the collection.
        This is confirmed: ``get_retriever()`` is the only caller of this method,
        and it always passes ``KNOWLEDGE_BASE``. The static-ID guard here is the
        second-line defence against runtime mutations to that list.
        """
        try:
            import chromadb
            from chromadb.utils import embedding_functions as ef
        except ImportError:
            log.debug("chromadb not installed — using TF-IDF retriever")
            return

        # Persistence guard: filter to static KB IDs only.
        static_docs = [d for d in docs if d["id"] in self._STATIC_KB_IDS]
        rejected = len(docs) - len(static_docs)
        if rejected:
            log.warning(
                "RAG: rejected %d non-static document(s) from chromadb indexing"
                " (agent-derived or unknown ID) — only static KNOWLEDGE_BASE"
                " entries are persisted",
                rejected,
            )

        client = chromadb.PersistentClient(path=str(self._dir))
        emb_fn = ef.DefaultEmbeddingFunction()
        # chromadb is mypy `Any` (mypy.ini: ignore_missing_imports + follow_imports=skip),
        # so this call is untyped to mypy and needs no `# type: ignore` — DefaultEmbeddingFunction
        # satisfies the runtime EmbeddingFunction protocol regardless of stub-generic variance.
        self._collection = client.get_or_create_collection(
            "slop_kb",
            embedding_function=emb_fn,
        )
        existing = set(self._collection.get()["ids"])
        to_add = [d for d in static_docs if d["id"] not in existing]
        if to_add:
            self._collection.add(
                ids=[d["id"] for d in to_add],
                documents=[f"{d['title']}\n{d['text']}" for d in to_add],
                metadatas=[{"title": d["title"]} for d in to_add],
            )
            log.info("RAG: indexed %d new documents into chromadb", len(to_add))
        self._ready = True

    def query(self, text: str, n: int = 3) -> list[str]:
        if not self._ready or not self._collection:
            return []
        try:
            results = self._collection.query(query_texts=[text], n_results=n)
            return results["documents"][0] if results["documents"] else []
        except Exception as e:
            log.debug("ChromaDB query failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Unified retriever — picks the best available backend
# ---------------------------------------------------------------------------

_retriever: SimpleRetriever | None = None
_chroma: ChromaRetriever | None = None
_built_at: float = 0.0
_REBUILD_INTERVAL = 3600  # rebuild index every hour


def get_retriever() -> SimpleRetriever | ChromaRetriever:
    """Return the best available retriever, building the index if needed."""
    global _retriever, _chroma, _built_at

    now = time.monotonic()
    if _retriever and (now - _built_at) < _REBUILD_INTERVAL:
        return _chroma if (_chroma and _chroma._ready) else _retriever

    # Try chromadb first
    try:
        from backend.core.config import config as _cfg

        chroma_dir = _cfg.data_dir / "rag" / "chroma"
        chroma_dir.mkdir(parents=True, exist_ok=True)
        _chroma = ChromaRetriever(chroma_dir)
        _chroma.build(KNOWLEDGE_BASE)
    except Exception as e:
        log.debug("ChromaDB init failed, using TF-IDF: %s", e)

    # Always build the TF-IDF fallback
    _retriever = SimpleRetriever()
    _retriever.build(KNOWLEDGE_BASE)
    _built_at = now

    log.debug("RAG retriever ready (%d documents)", len(KNOWLEDGE_BASE))
    if _chroma and _chroma._ready:
        return _chroma
    return _retriever


def query_knowledge_base(text: str, n: int = 3) -> list[str]:
    """Convenience function — query the knowledge base and return relevant chunks."""
    try:
        retriever = get_retriever()
        return retriever.query(text, n=n)
    except Exception as e:
        log.debug("RAG query failed: %s", e)
        return []


def enrich_prompt_with_context(prompt: str, error_text: str) -> str:
    """Inject relevant knowledge base chunks into an LLM prompt."""
    chunks = query_knowledge_base(error_text, n=3)
    if not chunks:
        return prompt
    context = "\n\n---\n".join(chunks)
    return f"Relevant knowledge base context:\n\n{context}\n\n---\n\n{prompt}"

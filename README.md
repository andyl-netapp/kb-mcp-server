# KB NetApp MCP Server — Setup Guide

Exposes [kb.netapp.com](https://kb.netapp.com) as AI tools for GitHub Copilot CLI, enabling natural-language KB article search and retrieval directly from your terminal. Authentication is handled automatically via SSO cookies captured through Microsoft Edge.

**Maintained by:** Cunliang (Andy) Li ([andy.li@netapp.com](mailto:andy.li@netapp.com))  
**Status:** Internal — contact Andy if you run into issues  
**Requires:** Corp VPN + GitHub Copilot CLI + Python 3.10+ + Microsoft Edge

> **Two versions available:**
> - **v2 (`kb_mcp_v2.py`) — Recommended.** Adds hybrid semantic search, exact keyword lookup, and a self-growing local article index.
> - **v1 (`kb_mcp.py`) — Legacy.** Functional but no longer actively developed. See [v1 section](#v1-legacy) below.

---

## v2 (Recommended)

### Files

| File | Description |
|------|-------------|
| `kb_mcp_v2.py` | MCP Server main program (v2) |
| `kb_client_v2.py` | Hybrid search + auto-indexing + reranking client |
| `build_index_http.py` | **Fast** bulk indexer — HTTP-based, no browser needed (~2–5s/article) |
| `build_index.py` | Playwright-based bulk indexer (legacy; slower, ~30–45s/article) |
| `requirements_v2.txt` | All Python dependencies (includes v1 deps) |
| `auth_manager.py` | Secure cookie storage (shared with v1) |
| `login_helper.py` | Browser-based SSO login (shared with v1) |
| `Set-KBCookies.ps1` | Login / cookie refresh script (shared with v1) |
| `Remove-KBCookies.ps1` | Cookie removal script (shared with v1) |

### What's new in v2

| Feature | v1 | v2 |
|---------|----|----|
| Keyword search across 18 KB categories | ✅ | ✅ |
| Natural-language / semantic search | ❌ | ✅ `kb_semantic_search` |
| Exact term / error code lookup | ❌ | ✅ `kb_keyword_lookup` |
| Metadata filters (domain, team, date) | ❌ | ✅ |
| Auto-index fetched articles | ❌ | ✅ |
| Anti-hallucination guardrail in responses | ❌ | ✅ |
| Cross-encoder reranking (MiniLM) | ❌ | ✅ |

### What can it do?

Once set up, you can ask Copilot CLI questions like:

- *"Search KB articles about FlexGroup rebalance performance"*
- *"Find KB articles about NFS latency on ONTAP 9.15"*
- *"Look up articles mentioning WAFL_CP_LIMIT"*
- *"What does KB article https://kb.netapp.com/... say about this error?"*
- *"Find articles about SnapMirror published after 2024-01-01"*

**v2 uses two complementary search modes:**
- **`kb_semantic_search`** — best for conceptual questions and natural-language troubleshooting descriptions. Uses BM25 + sentence-transformer cosine similarity, followed by cross-encoder reranking.
- **`kb_keyword_lookup`** — best for exact technical terms: error codes, EMS event names, ONTAP command names, log fragments (e.g. `ENOSPC`, `wafliron`, `snapmirror break`).

#### KB categories covered (18 total)

| Area | Team filter value |
|------|-------------------|
| Performance | `Performance` |
| Operating System | `OS` |
| Upgrade | `Upgrade` |
| MetroCluster | `MetroCluster` |
| Mediator | `Mediator` |
| Hardware | `Hardware` |
| NAS / SAN / XCP | `NAS`, `SAN`, `XCP` |
| SnapMirror / SnapLock / SnapRestore / NDMP | `SnapMirror`, `SnapLock`, `SnapRestore`, `NDMP` |
| FlexGroup / Encryption / Efficiency / FabricPool / System Manager | `FlexGroup`, `Encryption`, `Efficiency`, `FabricPool`, `SystemManager` |

### Step 1 — Install dependencies

```powershell
cd C:\Users\YOUR_USERNAME\apps\kb-mcp
pip install -r requirements_v2.txt
```

This installs all v1 dependencies plus: `sentence-transformers`, `numpy`, `rank-bm25`

> **First-run note:** On first use of `kb_semantic_search`, the server downloads two models:
> - `all-MiniLM-L6-v2` — embedding model (~90 MB)
> - `ms-marco-MiniLM-L-6-v2` — reranking cross-encoder (~85 MB)
>
> Both require internet access and are cached locally after the first download. Subsequent starts load from cache.

> **Corporate SSL note:** If you see `CERTIFICATE_VERIFY_FAILED` during model download, add the `KB_CA_BUNDLE` env var to your MCP config pointing to your corporate CA bundle:
> ```json
> "env": { "KB_CA_BUNDLE": "C:\\path\\to\\corporate-ca.pem" }
> ```

> **Note:** After `pip install`, you do **not** need to run `playwright install`. The server drives your existing system Edge instead.

### Step 2 — Log in to kb.netapp.com

Authentication is shared between v1 and v2 — if you have already logged in for v1, **no action needed**.

If setting up for the first time, see [Step 2 in the v1 section](#step-2--log-in-to-kbnetappcom-1) — the login process is identical.

### Step 3 — Add to your Copilot CLI MCP config

Find your config file (typically `~\.copilot\mcp-config.json`) and add the `kb-netapp-v2` block:

```json
{
  "mcpServers": {
    "kb-netapp-v2": {
      "command": "python",
      "args": ["C:\\Users\\YOUR_USERNAME\\apps\\kb-mcp\\kb_mcp_v2.py"],
      "env": {
        "KB_USERNAME": "YOUR_WINDOWS_USERNAME"
      }
    }
  }
}
```

> You can run v1 and v2 side-by-side — just add both blocks with different keys (`kb-netapp` and `kb-netapp-v2`).

Then restart Copilot CLI:
```
/restart
```

### Available tools

| Tool | What it does |
|------|-------------|
| `kb_check_auth` | Check if your session cookies are valid or expired |
| `kb_refresh_login` | Open Edge browser to refresh SSO cookies |
| `kb_semantic_search` | Natural-language hybrid search (BM25 + semantic) with optional domain/team/date filters |
| `kb_keyword_lookup` | Exact-match lookup for error codes, EMS events, command names |
| `kb_get_article` | Fetch full article content and auto-index it for future searches |
| `kb_fetch_url` | Fetch any kb.netapp.com page by URL |

### How the local index works

v2 maintains a local vector index at `~\.copilot\kb_index\article_index.pkl`. Every article fetched via `kb_get_article` is automatically chunked, embedded, and appended to this index — making future searches progressively smarter.

The indexing pipeline for each article:
1. **Fetch & parse** — downloads the article HTML and strips navigation chrome
2. **Header-aware chunking** — splits on Markdown/HTML headings first, then sub-splits oversized sections on paragraph breaks, targeting ~300 tokens (~1 200 chars) per chunk
3. **Context overlap** — prepends the last 15% of the previous chunk to each new chunk so no sentence is stranded without its lead-in
4. **Embedding** — encodes each chunk to a 384-dim vector using `all-MiniLM-L6-v2` and stores it alongside the raw text and metadata (URL, title, team, domain, date)
5. **Persist** — appends to the pickle index; duplicates are detected by URL and skipped

#### ⚠️ Current index status — Performance KB only

> The shared index shipped with this server contains **~747 chunks from ~694 Performance KB articles** — covering the entire `on-prem/ontap/Perf/` category. `kb_semantic_search` with `team: "Performance"` works fully out of the box.
>
> **All other KB teams have zero pre-indexed articles.** Searches against NAS, OS, SnapMirror, FlexGroup, etc. will only return articles individually fetched via `kb_get_article` since setup. To get full coverage for another area, run `build_index_http.py` as described below.

### Pre-building the index for other KB areas

**`build_index_http.py`** is the recommended bulk indexer — HTTP-based, no browser required, ~2–5 s/article.

#### Quick start

```powershell
# Performance is already indexed. Index NAS next:
python build_index_http.py --teams NAS

# Index multiple areas in one pass
python build_index_http.py --teams NAS,SnapMirror,FlexGroup

# Index every team (all 18 — allow 1–3 hours)
python build_index_http.py --teams Performance,OS,Upgrade,MetroCluster,Mediator,Hardware,NAS,SAN,XCP,SnapMirror,SnapLock,SnapRestore,NDMP,FlexGroup,Encryption,Efficiency,FabricPool,SystemManager

# Refresh a team to pick up articles added since last run
python build_index_http.py --teams NAS --force

# Quick smoke test — first 10 articles only
python build_index_http.py --teams NAS --limit 10
```

> **The index is additive** — running `build_index_http.py` twice never creates duplicates. Index whatever teams are relevant to your role; others can add theirs independently.

#### Team reference

| KB Area | `--teams` value | Status |
|---------|----------------|--------|
| Performance | `Performance` | ✅ Pre-indexed (~694 articles) |
| Operating System | `OS` | Not yet indexed |
| Upgrade | `Upgrade` | Not yet indexed |
| MetroCluster | `MetroCluster` | Not yet indexed |
| Mediator | `Mediator` | Not yet indexed |
| Hardware | `Hardware` | Not yet indexed |
| NAS (NFS/CIFS/SMB) | `NAS` | Not yet indexed |
| SAN (iSCSI/FC) | `SAN` | Not yet indexed |
| XCP | `XCP` | Not yet indexed |
| SnapMirror | `SnapMirror` | Not yet indexed |
| SnapLock | `SnapLock` | Not yet indexed |
| SnapRestore | `SnapRestore` | Not yet indexed |
| NDMP | `NDMP` | Not yet indexed |
| FlexGroup | `FlexGroup` | Not yet indexed |
| Encryption | `Encryption` | Not yet indexed |
| Efficiency | `Efficiency` | Not yet indexed |
| FabricPool | `FabricPool` | Not yet indexed |
| System Manager | `SystemManager` | Not yet indexed |

#### `build_index_http.py` options

| Option | Default | Description |
|--------|---------|-------------|
| `--teams` | `Performance` | Comma-separated team names to index |
| `--delay` | 0.3s | Pause between HTTP requests (be polite to the server) |
| `--limit` | none | Max articles per team (useful for testing) |
| `--force` | off | Re-index articles already in the index |

#### `build_index.py` — Legacy (Playwright-based)

The original Playwright-based indexer is still available for edge cases where HTTP misses JavaScript-rendered content. Requires `playwright install` and takes ~30–45 s/article.

---

## RAG Architecture

v2 implements a full **Retrieval-Augmented Generation (RAG)** pipeline. The retrieval layer lives entirely in `kb_client_v2.py`; the generation layer is the LLM (GitHub Copilot / Claude) that reads the retrieved chunks and produces an answer.

```
User Query
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  RETRIEVAL LAYER  (kb_client_v2.py)                     │
│                                                         │
│  Step 1 — Chunking  (at index time)                     │
│    • Header-aware semantic splitting                    │
│    • Sub-split oversized sections on paragraph breaks  │
│    • 15% context overlap prepended to each chunk       │
│    • ~300 tokens (~1200 chars) per chunk               │
│                                                         │
│  Step 2 — Hybrid Search  (at query time)               │
│    • BM25 keyword score (rank-bm25)                    │
│    • Embedding cosine similarity (all-MiniLM-L6-v2)   │
│    • Combined: score = 0.4×BM25_norm + 0.6×cosine     │
│    • Collect top-30 candidates (3× requested limit)    │
│                                                         │
│  Step 3 — Reranking  (at query time)                   │
│    • Cross-encoder: ms-marco-MiniLM-L-6-v2            │
│    • Re-scores each (query, title + snippet) pair      │
│    • Returns final top-10 by reranker score            │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  GENERATION LAYER  (LLM / GitHub Copilot)               │
│    • Reads retrieved chunks                             │
│    • Generates answer grounded in KB content only       │
│    • Strict guardrail: no hallucination                 │
└─────────────────────────────────────────────────────────┘
```

### BM25 — Keyword Relevance Scoring

**BM25 (Best Match 25)** is the gold-standard keyword retrieval algorithm, used by Elasticsearch, Solr, and most enterprise search engines. The "25" refers to the 25th iteration of hyperparameter tuning during its original development at City University London in the 1990s.

#### How it works

For a query `Q = {q₁, q₂, … qₙ}` and a document chunk `D`, BM25 computes:

```
BM25(Q, D) = Σᵢ  IDF(qᵢ) ×  TF(qᵢ, D) × (k₁ + 1)
                             ────────────────────────────────────
                             TF(qᵢ, D) + k₁ × (1 − b + b × |D|/avgdl)
```

| Parameter | Typical value | Effect |
|-----------|--------------|--------|
| **IDF(qᵢ)** | — | log-ratio of total chunks to chunks containing qᵢ. Rare terms score high; ubiquitous words like "ONTAP" contribute little. |
| **k₁** | ≈ 1.5 | Term-frequency saturation. Doubling a term's occurrence does NOT double its score — BM25 plateaus, preventing one repeated word from hijacking results. |
| **b** | ≈ 0.75 | Length normalization. Longer chunks are penalized slightly so a long article can't win just by repeating a term more times. |
| **\|D\|/avgdl** | — | Chunk length relative to average chunk length. |

#### What BM25 excels at

BM25 is unbeatable for exact technical terms with high IDF:
- Error codes: `ENOSPC`, `WAFL_CP_LIMIT`, `E0x00000010`
- ONTAP commands: `wafliron`, `snapmirror break`, `volume move`
- EMS event names: `wafl.scan.completed`, `raid.disk.missing`
- Internal service names: `kahuna`, `D-blade`, `waffinity`

#### BM25's weakness

BM25 has no concept of meaning. A query for *"slow metadata ops"* will not match an article about *"high latency during RENAME workload"* unless those exact words overlap. This is where semantic search compensates.

### Embedding — Semantic Vector Search

#### From words to geometry

Each text chunk is encoded into a **384-dimensional floating-point vector** using `all-MiniLM-L6-v2` at index time. At query time the same model encodes the query. **Cosine similarity** — the cosine of the angle between two vectors — measures semantic relatedness:

```
cos(θ) = (A · B) / (‖A‖ × ‖B‖)

  cos ≈ 1.0  →  nearly identical meaning
  cos ≈ 0.7  →  strongly related concepts
  cos ≈ 0.0  →  unrelated
  cos < 0.0  →  opposite meaning (rare in practice)
```

#### How `all-MiniLM-L6-v2` works

`all-MiniLM-L6-v2` is a **6-layer transformer encoder** (distilled from a larger BERT-family model) trained on 1 billion+ sentence pairs using **contrastive learning**:
- Semantically equivalent sentences are pushed *close together* in vector space
- Unrelated sentences are pushed *apart*
- Training data: NLI corpora, MS MARCO Q&A pairs, Reddit, Wikipedia, news articles

At inference: all token embeddings are **mean-pooled** into a single 384-dim vector regardless of input length. Vectors are L2-normalised so cosine similarity equals dot product — fast to compute.

#### What semantic search excels at

- **Synonym/paraphrase matching**: *"high latency"* ↔ *"slow response time"* ↔ *"increased IO delay"*
- **Natural-language questions**: *"why is my FlexGroup slow on renames"* → finds the RENAME workload KB even without those exact words
- **Conceptual proximity**: *"consistency point stall"* → surfaces articles mentioning `WAFL_CP_LIMIT` or `wafl.cp.defer`

#### Semantic search's weakness

Vector similarity can miss exact technical identifiers the model has not encountered during training. A query for `CONTAP-261527` (an internal bug ID) may not produce useful cosine similarity — the model has no embedding intuition for that string. BM25 handles these cases.

### Hybrid Scoring — Best of Both Worlds

```
final_score = 0.4 × BM25_normalized + 0.6 × cosine_similarity
```

Both BM25 and cosine scores are independently min-max normalized to [0, 1] before combining.

| Retrieval type | Strength | Weakness |
|----------------|----------|----------|
| BM25 only | Exact keywords, rare technical terms | No semantic understanding; misses paraphrases |
| Semantic only | Conceptual matching, natural-language queries | Misses exact rare terms; fooled by superficial similarity |
| **Hybrid (both)** | **Handles both query styles** | Requires both BM25 index and embedding vectors |

The **0.6/0.4 weighting toward semantics** is empirically tuned for KB troubleshooting queries, which are usually natural-language descriptions. The BM25 component acts as an anchor for lexical precision — when a query contains an exact ONTAP command or error code, BM25 boosts articles where that exact string appears.

The hybrid stage collects the **top 30 candidates** (3× the final requested count). This wider net ensures the cross-encoder reranker sees a diverse pool and doesn't miss a highly relevant article that ranked 11th in hybrid scoring.

### Reranking — Cross-Encoder Precision

#### Bi-encoder vs cross-encoder: the key difference

The embedding model (`all-MiniLM-L6-v2`) is a **bi-encoder**: it encodes the query and each document chunk *independently* into separate vectors, then compares them. This is fast (cache all document vectors once, encode only the query at search time) but sacrifices accuracy because the model never sees the query and document at the same time — it cannot model token-level interactions between them.

A **cross-encoder** takes query and candidate as a *single concatenated input*:

```
Input:  [CLS] <query tokens> [SEP] <document tokens> [SEP]
Output: one relevance score
```

Because every attention head can attend to *both* query and document simultaneously, the cross-encoder can model subtle signals the bi-encoder misses: negation (*"NOT an issue with X"*), conditionality (*"only when Y is configured"*), and exact phrase co-occurrence.

#### Why not use cross-encoder for everything?

Cross-encoders run a full forward pass for every (query, candidate) pair — they cannot pre-compute and cache document representations. With 747 indexed chunks that would mean 747 forward passes per query (~40 seconds). The **two-stage pipeline** gets the best of both worlds:

| Stage | Operation | Time | Reduces |
|-------|-----------|------|---------|
| Hybrid retrieval (bi-encoder + BM25) | Fast approximate scoring | ~10 ms | 747 chunks → 30 candidates |
| Cross-encoder reranking | Accurate pair scoring | ~200 ms | 30 candidates → top 10 |

#### Training data: MS MARCO

Both models are trained on (or distilled from) the **MS MARCO** dataset — 8.8 million real Bing search queries paired with relevant web passages. This trains the models for exactly our use case: short natural-language queries → long technical document retrieval.

| Stage | Model | Architecture | Size | Latency |
|-------|-------|-------------|------|---------|
| Embedding | `all-MiniLM-L6-v2` | Bi-encoder, 6-layer MiniLM, 384-dim | ~90 MB | ~5 ms/query |
| Reranking | `ms-marco-MiniLM-L-6-v2` | Cross-encoder, 6-layer MiniLM | ~85 MB | ~200 ms / 30 candidates |

Both models download automatically on first use and are cached in `~/.cache/huggingface/`.

---

## v1 (Legacy)

### Files

| File | Description |
|------|-------------|
| `kb_mcp.py` | MCP Server main program |
| `auth_manager.py` | Secure cookie storage module |
| `login_helper.py` | Browser-based SSO login module |
| `kb_client.py` | KB article search and fetch module |
| `Set-KBCookies.ps1` | Login / cookie refresh script |
| `Remove-KBCookies.ps1` | Cookie removal script |
| `requirements.txt` | Python dependencies |

### Step 1 — Install dependencies

```powershell
cd C:\Users\YOUR_USERNAME\apps\kb-mcp
pip install -r requirements.txt
```

This installs: `mcp`, `requests`, `beautifulsoup4`, `playwright`, `keyring`

> **Note:** After `pip install`, you do **not** need to run `playwright install`.

### Step 2 — Log in to kb.netapp.com

Each user needs to log in once to capture their own SSO session cookies. The cookies are stored securely and are personal — they cannot be shared.

#### Enable PowerShell script execution (one-time)

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

#### Run the login script

1. Open **PowerShell**
2. Navigate to the installation folder:
   ```powershell
   cd C:\Users\YOUR_USERNAME\apps\kb-mcp
   ```
3. Run:
   ```powershell
   .\Set-KBCookies.ps1
   ```
4. A **Microsoft Edge** window will open — complete the NetApp SSO login
5. The window closes automatically once login is detected

> **Session expiry:** Cookies typically last 8 hours. Re-run `.\Set-KBCookies.ps1` to refresh.

> **Important:** Close all other Microsoft Edge windows before running the login script.

### Step 3 — Add to your Copilot CLI MCP config

```json
{
  "mcpServers": {
    "kb-netapp": {
      "command": "python",
      "args": ["C:\\Users\\YOUR_USERNAME\\apps\\kb-mcp\\kb_mcp.py"],
      "env": {
        "KB_USERNAME": "YOUR_WINDOWS_USERNAME"
      }
    }
  }
}
```

### Available tools

| Tool | What it does |
|------|-------------|
| `kb_check_auth` | Check if your session cookies are valid or expired |
| `kb_refresh_login` | Open Edge browser to refresh SSO cookies |
| `kb_search` | Search KB articles by keyword, product, or category |
| `kb_get_article` | Fetch full article content by article ID or URL |
| `kb_fetch_url` | Fetch any kb.netapp.com page by URL |

---

## Refreshing your session (both versions)

```powershell
# Option A: PowerShell script (recommended)
.\Set-KBCookies.ps1

# Option B: from within Copilot CLI
# Ask: "Please refresh my KB login"  →  triggers kb_refresh_login tool
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Not logged in` or `No cookies stored` | Run `.\Set-KBCookies.ps1` |
| `Session cookies have expired` | Re-run `.\Set-KBCookies.ps1` |
| Edge window opens but MFA keeps spinning | Close all other Edge windows and try again |
| `Target page, context or browser has been closed` | Close all Edge windows and retry. If it persists, delete the browser profile: `Remove-Item -Recurse -Force "$env:USERPROFILE\.copilot\.netapp_browser_data"` |
| Tool not appearing in Copilot CLI | Check the `.py` path in config; run `/tools` to verify |
| Connection error to kb.netapp.com | Connect to **NetApp Corp VPN** first |
| `ModuleNotFoundError` on startup | Run `pip install -r requirements_v2.txt` (or `requirements.txt` for v1) |
| `CERTIFICATE_VERIFY_FAILED` on first v2 run | Set `KB_CA_BUNDLE` in MCP config env to your corporate CA bundle path |
| Article body shows "Sign in to view the entire content" despite being logged in | Known limitation — some KB articles use client-side JS rendering. Open the article directly in your browser instead. |
| Remove stored cookies | Run `.\Remove-KBCookies.ps1` |
| `kb_refresh_login` tool fails with encoding error | Known Windows `gbk` codec issue. Use `.\Set-KBCookies.ps1` in PowerShell instead |

---

## How cookie storage works

On NetApp Azure AD-joined machines, Windows Credential Manager may reject large data blobs due to Group Policy restrictions. The server handles this automatically:

1. **Primary:** Windows Credential Manager (keyring)
2. **Fallback:** DPAPI-encrypted file at `~\.copilot\.kb_cookies.dpapi`

Both methods are tied to your Windows user account and cannot be read by other users.

---

*Last updated by Andy Li — Internal use only — Do not share externally*


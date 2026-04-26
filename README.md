# KB NetApp MCP Server

Exposes [kb.netapp.com](https://kb.netapp.com) as AI tools for GitHub Copilot CLI, enabling natural-language KB article search and retrieval directly from your terminal.

**Maintained by:** Cunliang (Andy) Li ([andy.li@netapp.com](mailto:andy.li@netapp.com))  
**Status:** Internal — contact Andy if you run into issues  
**Requires:** Corp VPN + GitHub Copilot CLI + Python 3.10+ + Microsoft Edge

---

## Sharing with colleagues

Two sides — **both** are required.

### Andy's part — choose one option

**Option A — GitHub invite** _(preferred: colleague gets `git pull` updates automatically)_

1. Open https://github.com/andyl-netapp/kb-mcp-server
2. Click the **Settings** tab on the repo page (not the account Settings in the top-right corner)
3. In the left sidebar under **Access**, click **Collaborators** → **Add people**
4. Enter the colleague's GitHub username or email → **Add**
5. They'll get an email invite — they must accept it before they can clone

> Requires the colleague to have a GitHub account.

**Option B — Manual ZIP** _(fallback: no GitHub account needed)_

1. Open https://github.com/andyl-netapp/kb-mcp-server
2. Click the green **Code** button → **Download ZIP**
3. Send the ZIP to the colleague via Teams / email / OneDrive

> ⚠️ No automatic updates — when you push a fix, send them a new ZIP manually.

### Colleague's part

Point them to the **Setup** section below and tell them which option they're using (A or B).

---

## Files

| File | Description |
|------|-------------|
| `kb_mcp.py` | MCP Server main program |
| `kb_client.py` | Hybrid search + auto-indexing + reranking client |
| `build_index_http.py` | Bulk indexer — HTTP-based, ~2–5 s/article, no browser needed |
| `requirements.txt` | All Python dependencies |
| `auth_manager.py` | Secure cookie storage |
| `login_helper.py` | Browser-based SSO login |
| `Set-KBCookies.ps1` | Login / cookie refresh script |
| `Remove-KBCookies.ps1` | Cookie removal script |

---

## What can it do?

Once set up, you can ask Copilot CLI questions like:

- *"Search KB articles about FlexGroup rebalance performance"*
- *"Find KB articles about NFS latency on ONTAP 9.15"*
- *"Look up articles mentioning WAFL_CP_LIMIT"*
- *"What does KB article https://kb.netapp.com/... say about this error?"*

### This is a RAG system

This tool implements **RAG (Retrieval-Augmented Generation)** — a two-part pattern:

```
Your question
     │
     ▼
┌─────────────────────────────────┐
│  RETRIEVAL  (this tool)         │
│  BM25 + Semantic + Reranking    │
│  → top KB text chunks           │
└─────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────┐
│  GENERATION  (Copilot CLI LLM)  │
│  Claude / GPT reads the chunks  │
│  → generates a grounded answer  │
└─────────────────────────────────┘
```

Without RAG, the LLM answers from its training data alone — which may be outdated or missing NetApp-specific details. With RAG, every answer is grounded in real, current KB articles fetched from kb.netapp.com.

Two complementary search tools:

- **`kb_semantic_search`** — best for conceptual questions and natural-language troubleshooting descriptions.
- **`kb_keyword_lookup`** — best for exact technical terms: error codes, EMS event names, ONTAP command names, log fragments (e.g. `ENOSPC`, `wafliron`, `snapmirror break`).

### How `kb_semantic_search` works — three-stage retrieval pipeline

| Stage | Method | What it does |
|-------|--------|-------------|
| 1 | **BM25** | Fast sparse keyword retrieval — scores chunks by term frequency. Good at catching exact technical words even when semantic meaning is ambiguous. |
| 2 | **Semantic similarity** | Encodes your query and each candidate chunk into 384-dimensional vectors using `all-MiniLM-L6-v2`, then scores by cosine similarity. Catches meaning even when the exact words differ. |
| 3 | **Cross-encoder reranking** | Takes the top candidates from stages 1+2 and re-scores them by running the query and chunk *together* through `ms-marco-MiniLM-L-6-v2`. More accurate than vector similarity alone because it considers full query–chunk interaction. |

Stages 1+2 run in parallel over the full index (fast). Stage 3 only runs on the top ~50 candidates (slower but more precise). Final results are ordered by rerank score.

`kb_keyword_lookup` skips stages 2 and 3 — it does BM25 + exact string match only, optimised for looking up specific terms verbatim.

---

## Setup

Choose your path based on how Andy shared the files with you.

---

### Option A — GitHub (clone)

#### A-1. Accept the invite and clone

Check your email for a GitHub invitation from `andyl-netapp`. Accept it, then open PowerShell:

```powershell
git clone https://github.com/andyl-netapp/kb-mcp-server.git C:\Users\YOUR_USERNAME\apps\kb-mcp
```

_(replace `YOUR_USERNAME` with your actual Windows username, e.g. `jsmith`)_

To get future updates any time Andy pushes a fix:

```powershell
cd C:\Users\YOUR_USERNAME\apps\kb-mcp
git pull
```

#### A-2. Install dependencies

In the **same PowerShell window**, run:

```powershell
cd C:\Users\YOUR_USERNAME\apps\kb-mcp
pip install -r requirements.txt
```

You'll see many packages downloading — wait until the command prompt comes back.

> On first use of `kb_semantic_search`, two AI models (~175 MB total) are also downloaded and cached automatically. Cold start is ~11 seconds.

#### A-3. Log in to kb.netapp.com

In the **same PowerShell window**, run:

```powershell
python login_helper.py
```

A Microsoft Edge browser opens — complete the NetApp SSO login as normal. The window closes automatically. PowerShell prints `Cookies saved successfully`.

> Cookies last ~8 hours. When they expire, open PowerShell, `cd` to the `kb-mcp` folder, and run `python login_helper.py` again.  
> ⚠️ Do **not** use `kb_refresh_login` inside Copilot — it times out (MCP limit 30 s, login takes up to 5 min).

#### A-4. Add to Copilot CLI MCP config

1. Open **File Explorer** → navigate to `C:\Users\YOUR_USERNAME\.copilot\`
2. Right-click `mcp-config.json` → **Open with** → **Notepad**
3. Add the `kb-netapp` block inside the `"mcpServers": { }` section:

```json
{
  "mcpServers": {
    "kb-netapp": {
      "tools": ["*"],
      "command": "python",
      "args": ["C:\\Users\\YOUR_USERNAME\\apps\\kb-mcp\\kb_mcp.py"],
      "env": {
        "KB_USERNAME": "YOUR_WINDOWS_USERNAME",
        "KB_CA_BUNDLE": "C:\\Users\\YOUR_USERNAME\\.copilot\\ca-bundle.pem",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

> Replace every `YOUR_USERNAME` with your Windows username.  
> `KB_CA_BUNDLE` is required on NetApp-managed machines (prevents `CERTIFICATE_VERIFY_FAILED`).

4. Save the file
5. In Copilot CLI, type `/restart`
6. Type `/tools` — you should see `kb-netapp` tools listed ✅

---

### Option B — ZIP (no GitHub account needed)

#### B-1. Extract the ZIP

Extract the ZIP Andy sent you to a folder, for example:

```
C:\Users\YOUR_USERNAME\apps\kb-mcp\
```

#### B-2. Install dependencies

Press **Win + R**, type `powershell`, press Enter. Then run:

```powershell
cd C:\Users\YOUR_USERNAME\apps\kb-mcp
pip install -r requirements.txt
```

_(replace `YOUR_USERNAME` with your actual Windows username)_

You'll see many packages downloading — wait until the command prompt comes back.

> On first use of `kb_semantic_search`, two AI models (~175 MB total) are also downloaded and cached automatically. Cold start is ~11 seconds.

#### B-3. Log in to kb.netapp.com

In the **same PowerShell window**, run:

```powershell
python login_helper.py
```

A Microsoft Edge browser opens — complete the NetApp SSO login as normal. The window closes automatically. PowerShell prints `Cookies saved successfully`.

> Cookies last ~8 hours. When they expire, open PowerShell, `cd` to the `kb-mcp` folder, and run `python login_helper.py` again.  
> ⚠️ Do **not** use `kb_refresh_login` inside Copilot — it times out (MCP limit 30 s, login takes up to 5 min).

#### B-4. Add to Copilot CLI MCP config

1. Open **File Explorer** → navigate to `C:\Users\YOUR_USERNAME\.copilot\`
2. Right-click `mcp-config.json` → **Open with** → **Notepad**
3. Add the `kb-netapp` block inside the `"mcpServers": { }` section:

```json
{
  "mcpServers": {
    "kb-netapp": {
      "tools": ["*"],
      "command": "python",
      "args": ["C:\\Users\\YOUR_USERNAME\\apps\\kb-mcp\\kb_mcp.py"],
      "env": {
        "KB_USERNAME": "YOUR_WINDOWS_USERNAME",
        "KB_CA_BUNDLE": "C:\\Users\\YOUR_USERNAME\\.copilot\\ca-bundle.pem",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

> Replace every `YOUR_USERNAME` with your Windows username.  
> `KB_CA_BUNDLE` is required on NetApp-managed machines (prevents `CERTIFICATE_VERIFY_FAILED`).

4. Save the file
5. In Copilot CLI, type `/restart`
6. Type `/tools` — you should see `kb-netapp` tools listed ✅

---

## Available tools

| Tool | What it does |
|------|-------------|
| `kb_check_auth` | Check if session cookies are valid or expired |
| `kb_refresh_login` | Open browser to refresh SSO cookies (use PowerShell, not Copilot) |
| `kb_semantic_search` | Natural-language hybrid search with optional domain/team/date filters |
| `kb_keyword_lookup` | Exact-match lookup for error codes, EMS events, command names |
| `kb_get_article` | Fetch full article content and auto-index it for future searches |
| `kb_fetch_url` | Fetch any kb.netapp.com page by URL |

---

## Local Index

The server maintains a local vector index at `~\.copilot\kb_index\article_index.pkl`. Every article fetched via `kb_get_article` is automatically chunked, embedded, and appended — making future searches progressively smarter.

### Current index status

> The index currently contains **1,285 chunks from 1,092 Performance KB articles** (fully indexed). `kb_semantic_search` with `team: "Performance"` works fully out of the box.
>
> FlexGroup has 9 articles indexed. All other KB teams (NAS, OS, SnapMirror, etc.) are not yet pre-indexed. Run `build_index_http.py` to add them.

### Pre-building the index for other KB areas

> **Prerequisites before running:**
> 1. Open PowerShell and `cd` to your `kb-mcp` folder
> 2. Make sure you are logged in first — run `python login_helper.py` if needed
> 3. Stay connected to NetApp Corp VPN throughout

> **⚠️ The index is local to your machine.** Each person must build their own. There is no shared index — the `.pkl` file lives at `~\.copilot\kb_index\article_index.pkl` on your own PC.

```powershell
# Index NAS articles
python build_index_http.py --teams NAS

# Index multiple areas in one pass
python build_index_http.py --teams NAS,SnapMirror,FlexGroup

# Index all 18 teams (allow 1–3 hours)
python build_index_http.py --teams Performance,OS,Upgrade,MetroCluster,Mediator,Hardware,NAS,SAN,XCP,SnapMirror,SnapLock,SnapRestore,NDMP,FlexGroup,Encryption,Efficiency,FabricPool,SystemManager

# Refresh a team after new articles are published
python build_index_http.py --teams NAS --force
```

### Team reference

| KB Area | `--teams` value | Status |
|---------|----------------|--------|
| Performance | `Performance` | ✅ Pre-indexed (1,092 articles) |
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
| FlexGroup | `FlexGroup` | ✅ Partially indexed (9 articles) |
| Encryption | `Encryption` | Not yet indexed |
| Efficiency | `Efficiency` | Not yet indexed |
| FabricPool | `FabricPool` | Not yet indexed |
| System Manager | `SystemManager` | Not yet indexed |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Not logged in` / `No cookies stored` | Run `python login_helper.py` from PowerShell |
| `Session cookies have expired` | Re-run `python login_helper.py` |
| `kb_refresh_login` times out | Expected — always use `python login_helper.py` directly |
| Tool not appearing in Copilot | Check path in config; run `/tools` to verify |
| Connection error to kb.netapp.com | Connect to NetApp Corp VPN first |
| `ModuleNotFoundError` on startup | Run `pip install -r requirements.txt` |
| `CERTIFICATE_VERIFY_FAILED` | Set `KB_CA_BUNDLE` env var to your corporate CA bundle path |

---

*Internal use only — Do not share externally*

import os
import re
import uuid
import sqlite3
import hashlib
import logging
import threading
import concurrent.futures
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI
import frontmatter

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notes-rag-mcp")

# Environment configurations
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000/v1")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "dummy")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "notes_rag")
VAULT_PATH = os.getenv("VAULT_PATH", "/containers/productivity/obsidian/shared")
CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "/app/data/index_cache.db")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# Thread locking and indexing status flag
indexing_lock = threading.Lock()
is_indexing = False

# Initialize clients
logger.info(f"Connecting to Qdrant at {QDRANT_URL} and LiteLLM at {LITELLM_URL}")
qdrant = QdrantClient(url=QDRANT_URL)
openai_client = OpenAI(base_url=LITELLM_URL, api_key=LITELLM_API_KEY)

# Ensure data directory exists
os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)

# Cache database setup
def get_db_connection():
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_cache_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_files (
                filepath TEXT PRIMARY KEY,
                mtime REAL,
                hash TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE,
                type TEXT, -- "directory" or "file"
                recursive INTEGER, -- 1 or 0
                enabled INTEGER DEFAULT 1,
                category TEXT, -- Optional override
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

        # Seed default vault path if the configuration database has /notes or is empty
        try:
            # Delete old '/notes' path if it exists to clean up
            conn.execute("DELETE FROM indexed_paths WHERE path = '/notes'")
            conn.commit()
            
            count = conn.execute("SELECT count(*) FROM indexed_paths").fetchone()[0]
            if count == 0:
                logger.info(f"Seeding default vault path: {VAULT_PATH}")
                conn.execute(
                    "INSERT OR IGNORE INTO indexed_paths (path, type, recursive, enabled, category) VALUES (?, ?, ?, ?, ?)",
                    (os.path.abspath(VAULT_PATH), "directory", 1, 1, "default")
                )
                conn.commit()
        except Exception as se:
            logger.error(f"Failed to seed default path: {se}")

init_cache_db()

def set_metadata(key, value):
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO system_metadata (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to set metadata key {key}: {e}")

# Generate UUID deterministically
def get_chunk_uuid(rel_path: str, index: int) -> str:
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, "notes-rag-mcp.lan")
    return str(uuid.uuid5(namespace, f"{rel_path}#{index}"))

# Retrieve Embeddings
def get_embedding(text: str) -> List[float]:
    response = openai_client.embeddings.create(
        input=text,
        model=EMBEDDING_MODEL
    )
    return response.data[0].embedding

# Ensure collection exists in Qdrant
def ensure_collection():
    try:
        # Fetch dimensions dynamically
        logger.info("Determining embedding dimensions from model...")
        sample_emb = get_embedding("test")
        dim = len(sample_emb)
        logger.info(f"Dimension identified as: {dim}")
    except Exception as e:
        logger.error(f"Failed to fetch sample embedding: {e}. Defaulting dimensions to 1536.")
        dim = 1536

    try:
        if qdrant.collection_exists(COLLECTION_NAME):
            info = qdrant.get_collection(COLLECTION_NAME)
            vectors_config = info.config.params.vectors
            current_dim = None
            if hasattr(vectors_config, "size"):
                current_dim = vectors_config.size
            elif isinstance(vectors_config, dict) and "size" in vectors_config:
                current_dim = vectors_config["size"]
            
            if current_dim is not None and current_dim != dim:
                logger.warning(f"Collection dimension mismatch: expected {dim}, found {current_dim}. Recreating collection...")
                qdrant.delete_collection(COLLECTION_NAME)

        if not qdrant.collection_exists(COLLECTION_NAME):
            logger.info(f"Creating Qdrant collection: {COLLECTION_NAME}")
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=dim,
                    distance=qmodels.Distance.COSINE
                )
            )
        else:
            logger.info(f"Collection {COLLECTION_NAME} already exists with correct dimensions.")
    except Exception as e:
        logger.error(f"Error checking/creating Qdrant collection: {e}")


# Markdown Chunking Logic
def split_by_length(text: str, heading: str, max_chars: int, overlap: int) -> List[Dict[str, str]]:
    if len(text) <= max_chars:
        return [{"heading": heading, "content": text}]
    
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunk_text = text[start:end]
        chunks.append({"heading": heading, "content": chunk_text})
        start += max_chars - overlap
        if start >= len(text) - overlap:
            break
    return chunks

def chunk_markdown(text: str, max_chars: int = 1500, overlap: int = 200) -> List[Dict[str, str]]:
    lines = text.split("\n")
    chunks = []
    current_heading = "Root"
    current_section = []

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    for line in lines:
        match = heading_pattern.match(line)
        if match:
            if current_section:
                section_text = "\n".join(current_section)
                chunks.extend(split_by_length(section_text, current_heading, max_chars, overlap))
                current_section = []
            current_heading = match.group(2).strip()
            current_section.append(line)
        else:
            current_section.append(line)

    if current_section:
        section_text = "\n".join(current_section)
        chunks.extend(split_by_length(section_text, current_heading, max_chars, overlap))

    return chunks

# Parallel file processor worker
def process_file_worker(filepath: str, category_override: Optional[str]) -> tuple:
    try:
        mtime = os.path.getmtime(filepath)
        
        # Check cache database and verify Qdrant point existence
        cache_valid = False
        with get_db_connection() as conn:
            row = conn.execute("SELECT mtime FROM indexed_files WHERE filepath = ?", (filepath,)).fetchone()
            if row and abs(row["mtime"] - mtime) < 0.01:
                # Double check that Qdrant actually has points for this path
                try:
                    scroll_res = qdrant.scroll(
                        collection_name=COLLECTION_NAME,
                        scroll_filter=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="path",
                                    match=qmodels.MatchValue(value=filepath)
                                )
                            ]
                        ),
                        limit=1,
                        with_payload=False,
                        with_vectors=False
                    )
                    if scroll_res and scroll_res[0]:
                        cache_valid = True
                except Exception as qe:
                    logger.error(f"Error checking Qdrant for {filepath}: {qe}")
                    
        if cache_valid:
            return (filepath, mtime, [], "skipped")
            
        logger.info(f"Indexing RAG file: {filepath}")
        
        # Read text content
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            text_content = f.read()
            
        meta = {}
        content = text_content
        
        # Attempt to parse YAML frontmatter if it is markdown/txt
        if filepath.endswith((".md", ".txt")):
            try:
                post = frontmatter.loads(text_content)
                content = post.content
                meta = post.metadata or {}
            except Exception:
                pass
                
        folder = os.path.basename(os.path.dirname(filepath))
        if not folder:
            folder = "root"
            
        title = os.path.basename(filepath)
        category = category_override or meta.get("category", folder)
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        elif not isinstance(tags, list):
            tags = []
            
        # Chunk and index
        chunks = chunk_markdown(content, max_chars=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        points = []
        for idx, chunk in enumerate(chunks):
            chunk_content = chunk["content"].strip()
            if not chunk_content:
                continue
            
            point_id = get_chunk_uuid(filepath, idx)
            vector = get_embedding(chunk_content)
            
            points.append(qmodels.PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "path": filepath,
                    "folder": folder,
                    "title": title,
                    "category": category,
                    "tags": tags,
                    "heading": chunk["heading"],
                    "content": chunk["content"]
                }
            ))
        return (filepath, mtime, points, "indexed")
    except Exception as e:
        logger.error(f"Failed to process file {filepath}: {e}")
        return (filepath, None, [], "failed")

# Incremental Directory Indexing
def run_indexing():
    global is_indexing
    if not indexing_lock.acquire(blocking=False):
        logger.warning("Indexing already in progress. Skipping duplicate scan request.")
        return False
        
    is_indexing = True
    try:
        logger.info("Starting incremental scan of custom RAG paths...")
        ensure_collection()
        
        # Load custom configured paths
        active_paths = []
        with get_db_connection() as conn:
            rows = conn.execute("SELECT path, type, recursive, category FROM indexed_paths WHERE enabled = 1").fetchall()
            for r in rows:
                active_paths.append({
                    "path": r["path"],
                    "type": r["type"],
                    "recursive": bool(r["recursive"]),
                    "category": r["category"]
                })
                
        # Backwards compatibility fallback if nothing is configured
        if not active_paths:
            logger.info("No custom RAG paths configured in database. Scanning default notes vault: %s", VAULT_PATH)
            active_paths.append({
                "path": VAULT_PATH,
                "type": "directory",
                "recursive": True,
                "category": None
            })
            
        found_files = {} # Maps absolute_filepath -> category_override
        supported_extensions = (".md", ".txt", ".yaml", ".yml", ".conf", ".json", ".py", ".cs", ".sh", ".xml", ".csproj", ".html", ".css", ".js")
        
        for target in active_paths:
            tpath = target["path"]
            ttype = target["type"]
            category_override = target["category"]
            
            if not os.path.exists(tpath):
                logger.warning(f"Indexed path does not exist on disk, skipping: {tpath}")
                continue
                
            if ttype == "file":
                if tpath.endswith(supported_extensions):
                    found_files[os.path.abspath(tpath)] = category_override
            else: # directory
                recursive = target["recursive"]
                if recursive:
                    for root, dirs, files in os.walk(tpath):
                        # Skip hidden directories in-place (e.g. .obsidian, .git, .trash)
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for file in files:
                            if file.startswith("."):
                                continue
                            if file.endswith(supported_extensions):
                                full_path = os.path.abspath(os.path.join(root, file))
                                found_files[full_path] = category_override
                else:
                    for entry in os.scandir(tpath):
                        if entry.name.startswith("."):
                            continue
                        if entry.is_file() and entry.name.endswith(supported_extensions):
                            full_path = os.path.abspath(entry.path)
                            found_files[full_path] = category_override

        indexed_count = 0
        skipped_count = 0
        failed_count = 0

        all_new_points = []
        files_to_update_cache = [] # List of (filepath, mtime)
        files_to_delete_from_qdrant = [] # List of filepath

        # Process files concurrently in a thread pool (max 8 workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(process_file_worker, filepath, cat): filepath 
                for filepath, cat in found_files.items()
            }
            for future in concurrent.futures.as_completed(futures):
                filepath, mtime, points, status = future.result()
                if status == "skipped":
                    skipped_count += 1
                elif status == "failed":
                    failed_count += 1
                elif status == "indexed":
                    indexed_count += 1
                    files_to_delete_from_qdrant.append(filepath)
                    if points:
                        all_new_points.extend(points)
                    files_to_update_cache.append((filepath, mtime))

        # Perform deletions from Qdrant in batches
        for filepath in files_to_delete_from_qdrant:
            try:
                qdrant.delete(
                    collection_name=COLLECTION_NAME,
                    points_selector=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="path",
                                match=qmodels.MatchValue(value=filepath)
                            )
                        ]
                    )
                )
            except Exception as e:
                logger.error(f"Failed to delete old points for {filepath}: {e}")

        # Bulk upsert new points to Qdrant
        if all_new_points:
            logger.info(f"Upserting {len(all_new_points)} vector chunks in bulk to Qdrant...")
            try:
                qdrant.upsert(collection_name=COLLECTION_NAME, points=all_new_points)
            except Exception as e:
                logger.error(f"Failed to bulk upsert points to Qdrant: {e}")

        # Batch update SQLite Cache in a single write transaction
        if files_to_update_cache:
            try:
                with get_db_connection() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO indexed_files (filepath, mtime) VALUES (?, ?)",
                        files_to_update_cache
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"Failed to write cache updates to SQLite: {e}")

        # Clean up deleted files from cache & Qdrant
        deleted_count = 0
        with get_db_connection() as conn:
            all_cached = [row["filepath"] for row in conn.execute("SELECT filepath FROM indexed_files").fetchall()]
            
        for cached_path in all_cached:
            if cached_path not in found_files:
                logger.info(f"Removing deleted file from RAG index: {cached_path}")
                try:
                    qdrant.delete(
                        collection_name=COLLECTION_NAME,
                        points_selector=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="path",
                                    match=qmodels.MatchValue(value=cached_path)
                                )
                            ]
                        )
                    )
                    with get_db_connection() as conn:
                        conn.execute("DELETE FROM indexed_files WHERE filepath = ?", (cached_path,))
                        conn.commit()
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Failed to remove deleted file {cached_path} from Qdrant: {e}")

        logger.info(f"Indexing completed. Indexed: {indexed_count}, Skipped: {skipped_count}, Deleted: {deleted_count}, Failed: {failed_count}")
        import datetime
        last_indexed_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_metadata("last_indexed", last_indexed_time)
        return True
    finally:
        is_indexing = False
        indexing_lock.release()

# Initialize MCP server
mcp_server = Server("notes-rag-mcp")

@mcp_server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="search_notes",
            description="Perform semantic search on your markdown notes and documentation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The natural language query or question."},
                    "folder": {"type": "string", "description": "Optional subdirectory to filter by (e.g. Infrastructure, Books)."},
                    "tag": {"type": "string", "description": "Optional frontmatter tag to filter by."},
                    "category": {"type": "string", "description": "Optional frontmatter category to filter by."},
                    "limit": {"type": "integer", "description": "Max number of search results to return.", "default": 5}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="trigger_reindex",
            description="Force an immediate directory scan to index new/updated markdown files.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="index_status",
            description="Get indexing statistics of your notes vault.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]

@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    if name == "search_notes":
        query = arguments["query"]
        folder = arguments.get("folder")
        tag = arguments.get("tag")
        category = arguments.get("category")
        limit = arguments.get("limit", 5)

        logger.info(f"Received search: query='{query}', folder={folder}, tag={tag}, category={category}")
        
        if not query.strip():
            return [TextContent(type="text", text="Error: search query cannot be empty.")]

        try:
            # Generate query embedding
            query_vector = get_embedding(query.strip())
            
            # Construct Qdrant filters
            must_conditions = []
            if folder:
                must_conditions.append(qmodels.FieldCondition(key="folder", match=qmodels.MatchValue(value=folder)))
            if category:
                must_conditions.append(qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=category)))
            if tag:
                # Assuming tags is an array of strings in Qdrant payload
                must_conditions.append(qmodels.FieldCondition(key="tags", match=qmodels.MatchAny(any=[tag])))

            query_filter = qmodels.Filter(must=must_conditions) if must_conditions else None

            # Perform search
            search_results = qdrant.search(
                collection_name=COLLECTION_NAME,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=limit
            )

            # Format outputs
            if not search_results:
                return [TextContent(type="text", text="No matching documents found.")]
            
            formatted_chunks = []
            for hit in search_results:
                p = hit.payload
                tags_str = ", ".join(p.get("tags", []))
                header_info = f" -> {p['heading']}" if p.get("heading") and p["heading"] != "Root" else ""
                
                meta_block = f"File: {p['path']}{header_info}\n"
                if p.get("category"):
                    meta_block += f"Category: {p['category']}\n"
                if tags_str:
                    meta_block += f"Tags: {tags_str}\n"
                meta_block += f"Score: {hit.score:.4f}\n"
                
                formatted_chunks.append(f"{meta_block}---\n{p['content']}\n\n========================")

            output_text = "\n\n".join(formatted_chunks)
            return [TextContent(type="text", text=output_text)]

        except Exception as e:
            logger.error(f"Search failed: {e}")
            return [TextContent(type="text", text=f"Error performing search: {str(e)}")]

    elif name == "trigger_reindex":
        try:
            run_indexing()
            return [TextContent(type="text", text="Reindexing completed successfully.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Reindexing failed: {str(e)}")]

    elif name == "index_status":
        try:
            with get_db_connection() as conn:
                files_count = conn.execute("SELECT count(*) FROM indexed_files").fetchone()[0]
            
            total_vectors = 0
            if qdrant.collection_exists(COLLECTION_NAME):
                info = qdrant.get_collection(COLLECTION_NAME)
                total_vectors = info.points_count

            status_text = (
                f"Vault Path: {VAULT_PATH}\n"
                f"Total Files Indexed: {files_count}\n"
                f"Total Vector Chunks Stored: {total_vectors}\n"
                f"Vector Collection: {COLLECTION_NAME}\n"
                f"LiteLLM Embedding Model: {EMBEDDING_MODEL}"
            )
            return [TextContent(type="text", text=status_text)]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to gather status: {str(e)}")]

    else:
        raise ValueError(f"Unknown tool name: {name}")

# FastAPI setup to support SSE
app = FastAPI(title="Notes RAG MCP Server")
sse_transport = SseServerTransport("/messages")

# ----------------------------------------------------
# ADMIN DASHBOARD API ENDPOINTS
# ----------------------------------------------------

@app.get("/admin/api/stats")
async def get_stats():
    try:
        with get_db_connection() as conn:
            files_count = conn.execute("SELECT count(*) FROM indexed_files").fetchone()[0]
            paths_count = conn.execute("SELECT count(*) FROM indexed_paths").fetchone()[0]
            row = conn.execute("SELECT value FROM system_metadata WHERE key = 'last_indexed'").fetchone()
            last_indexed_time = row["value"] if row else "Never"
        
        points_count = 0
        if qdrant.collection_exists(COLLECTION_NAME):
            info = qdrant.get_collection(COLLECTION_NAME)
            points_count = info.points_count
            
        return {
            "paths_count": paths_count,
            "files_count": files_count,
            "points_count": points_count,
            "is_indexing": is_indexing,
            "last_indexed": last_indexed_time
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/admin/api/paths")
async def get_paths():
    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT id, path, type, recursive, enabled, category, added_at FROM indexed_paths ORDER BY added_at DESC").fetchall()
            paths = [dict(r) for r in rows]
        return paths
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/admin/api/paths")
async def add_path(request: Request):
    try:
        data = await request.json()
        path = data.get("path")
        ptype = data.get("type", "directory")
        recursive = int(data.get("recursive", 1))
        enabled = int(data.get("enabled", 1))
        category = data.get("category")
        
        if not path:
            return JSONResponse(status_code=400, content={"error": "Path is required"})
            
        if not os.path.exists(path):
            return JSONResponse(status_code=400, content={"error": f"Path does not exist on disk: {path}"})
            
        # Standardize path
        path = os.path.abspath(path)
            
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO indexed_paths (path, type, recursive, enabled, category) VALUES (?, ?, ?, ?, ?)",
                (path, ptype, recursive, enabled, category)
            )
            conn.commit()
            
        # Trigger indexing in background
        threading.Thread(target=run_indexing, daemon=True).start()
        
        return {"status": "success", "message": f"Added path: {path}"}
    except sqlite3.IntegrityError:
        return JSONResponse(status_code=400, content={"error": "Path is already registered"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.put("/admin/api/paths/{path_id}")
async def update_path(path_id: int, request: Request):
    try:
        data = await request.json()
        recursive = int(data.get("recursive", 1))
        enabled = int(data.get("enabled", 1))
        category = data.get("category")
        
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE indexed_paths SET recursive = ?, enabled = ?, category = ? WHERE id = ?",
                (recursive, enabled, category, path_id)
            )
            conn.commit()
            
        # Trigger indexing in background
        threading.Thread(target=run_indexing, daemon=True).start()
        
        return {"status": "success", "message": f"Updated path ID: {path_id}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/admin/api/paths/{path_id}")
async def delete_path(path_id: int):
    try:
        # Get path details first
        with get_db_connection() as conn:
            row = conn.execute("SELECT path FROM indexed_paths WHERE id = ?", (path_id,)).fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Path not found"})
            path = row["path"]
            
            # Remove from path db
            conn.execute("DELETE FROM indexed_paths WHERE id = ?", (path_id,))
            conn.commit()
            
        # Trigger reindexing in background (this will automatically clean up deleted files)
        threading.Thread(target=run_indexing, daemon=True).start()
        
        return {"status": "success", "message": f"Deleted path: {path}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/admin/api/reindex")
async def trigger_reindex():
    if is_indexing:
        return JSONResponse(status_code=409, content={"error": "Indexing is already in progress"})
        
    threading.Thread(target=run_indexing, daemon=True).start()
    return {"status": "success", "message": "Indexing triggered in background"}

@app.get("/admin/api/browse")
async def browse_directory(path: str = "/containers"):
    resolved = os.path.abspath(path)
    if not resolved.startswith("/containers"):
        resolved = "/containers"
        
    if not os.path.exists(resolved):
        return JSONResponse(status_code=400, content={"error": f"Path '{path}' does not exist"})
        
    try:
        entries = os.scandir(resolved)
        dirs = []
        files = []
        for entry in entries:
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": os.path.abspath(entry.path)})
                else:
                    files.append({"name": entry.name, "path": os.path.abspath(entry.path)})
            except Exception:
                pass
                
        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        
        parent = os.path.dirname(resolved)
        if resolved in ("/containers", "/"):
            parent = ""
            
        return {
            "current_path": resolved,
            "parent_path": parent,
            "directories": dirs,
            "files": files
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/admin/")

# Mount Static Files (serving the index.html page)
app.mount("/admin", StaticFiles(directory="www", html=True), name="admin")

@app.get("/sse")
async def sse_endpoint(request: Request):
    logger.info("New SSE client connection requested.")
    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options()
        )

@app.post("/messages")
async def messages_endpoint(request: Request):
    await sse_transport.handle_post_request(request.scope, request.receive, request._send)

# Health check endpoint
@app.get("/health")
async def health():
    return JSONResponse(content={"status": "healthy"})

# Run initial indexing on startup
@app.on_event("startup")
async def startup_event():
    logger.info("Server starting up...")
    try:
        threading.Thread(target=run_indexing, daemon=True).start()
    except Exception as e:
        logger.error(f"Error running initial index on startup: {e}")

if __name__ == "__main__":
    import uvicorn
    # Expose on all interfaces on port 3000
    uvicorn.run(app, host="0.0.0.0", port=3000)

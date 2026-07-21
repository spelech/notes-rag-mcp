import os
import re
import json
import uuid
import sqlite3
import hashlib
import logging
import threading
import concurrent.futures
from collections import Counter
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent, Resource, Prompt, PromptMessage, PromptArgument
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI
import frontmatter

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notes-rag-mcp")

# Environment configurations
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000/v1")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "dummy")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "notes_rag")
VAULT_PATH = os.getenv("VAULT_PATH", "/containers/productivity/obsidian/shared")
CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "/app/data/index_cache.db")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# Thread locking and indexing status flag
indexing_lock = threading.Lock()
is_indexing = False

# Initialize clients
logger.info(f"Connecting to Qdrant at {QDRANT_URL}")
qdrant = QdrantClient(url=QDRANT_URL)

fastembed_model = None
fastembed_lock = threading.Lock()
openai_client = None

if EMBEDDING_PROVIDER == "local":
    try:
        from fastembed import TextEmbedding
        logger.info(f"Initializing FastEmbed local ONNX model: {EMBEDDING_MODEL}")
        fastembed_model = TextEmbedding(model_name=EMBEDDING_MODEL)
        logger.info("FastEmbed model initialized successfully.")
    except Exception as fe_err:
        logger.error(f"Failed to initialize FastEmbed model '{EMBEDDING_MODEL}': {fe_err}. Falling back to API.")
        EMBEDDING_PROVIDER = "api"

if EMBEDDING_PROVIDER == "api":
    logger.info(f"Using OpenAI/LiteLLM API embeddings at {LITELLM_URL} model {EMBEDDING_MODEL}")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_summaries (
                filepath TEXT PRIMARY KEY,
                title TEXT,
                folder TEXT,
                category TEXT,
                tags TEXT,
                headings TEXT,
                keywords TEXT,
                mtime REAL
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
    if EMBEDDING_PROVIDER == "local" and fastembed_model is not None:
        with fastembed_lock:
            embeddings = list(fastembed_model.embed([text]))
            return embeddings[0].tolist()
    else:
        if openai_client is None:
            raise RuntimeError("OpenAI client not initialized.")
        response = openai_client.embeddings.create(
            input=text,
            model=EMBEDDING_MODEL
        )
        return response.data[0].embedding


# Stopwords for keyword extraction
STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by",
    "can", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing",
    "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself",
    "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is",
    "isn't", "it", "it's", "its", "itself", "let's", "me", "more", "most", "mustn't", "my", "myself",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours",
    "ourselves", "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's", "should",
    "shouldn't", "so", "some", "such", "than", "that", "that's", "the", "their", "theirs", "them",
    "themselves", "then", "there", "there's", "these", "they", "they'd", "they'll", "they're",
    "they've", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "wasn't",
    "we", "we'd", "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's",
    "where", "where's", "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't",
    "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself",
    "yourselves", "true", "false", "none", "null", "file", "path", "type", "http", "https", "com",
    "net", "org", "yaml", "json", "txt", "md", "root"
}

def extract_keywords_from_text(content: str, title: str, headings: List[str], tags: List[str]) -> List[str]:
    combined = f"{title} {' '.join(headings)} {' '.join(tags)} {content}".lower()
    words = re.findall(r'[a-zA-Z0-9_\-]+', combined)
    filtered = [w for w in words if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()]
    counts = Counter(filtered)
    return [w for w, c in counts.most_common(25)]

def get_dynamic_search_tool_description() -> str:
    base_desc = "Perform semantic search across system documentation, notes, and codebase files."
    try:
        with get_db_connection() as conn:
            rows = conn.execute("SELECT title, category, tags, keywords FROM file_summaries").fetchall()
            
        if not rows:
            return base_desc + " Search by natural language query, folder, tag, or category."

        all_titles = []
        all_categories = set()
        all_tags = set()
        keyword_counts = {}

        for r in rows:
            t = r["title"]
            if t and t not in all_titles:
                all_titles.append(t)
            if r["category"]:
                all_categories.add(r["category"])
            
            try:
                tags_list = json.loads(r["tags"]) if r["tags"] else []
                for tag in tags_list:
                    all_tags.add(tag)
            except Exception:
                pass

            try:
                kw_list = json.loads(r["keywords"]) if r["keywords"] else []
                for kw in kw_list:
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
            except Exception:
                pass

        top_titles = [t for t in all_titles if not t.startswith(".")]
        top_keywords = sorted(keyword_counts.keys(), key=lambda k: keyword_counts[k], reverse=True)[:25]
        
        parts = [base_desc]
        if top_titles:
            parts.append(f"Indexed Documents: {', '.join(top_titles[:15])}.")
        if all_categories or all_tags:
            cats_tags = list(all_categories) + list(all_tags)
            parts.append(f"Categories & Tags: {', '.join(cats_tags[:15])}.")
        if top_keywords:
            parts.append(f"Key Concepts & Topics: {', '.join(top_keywords[:25])}.")

        return " ".join(parts)
    except Exception as e:
        logger.error(f"Error building dynamic tool description: {e}")
        return base_desc + " Search by natural language query, folder, tag, or category."


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
            # Check if file_summaries table already has this file
            with get_db_connection() as conn:
                sum_row = conn.execute("SELECT filepath FROM file_summaries WHERE filepath = ?", (filepath,)).fetchone()
            if sum_row:
                return (filepath, mtime, [], "skipped", None)
                
            # If summary is missing, parse metadata quickly without generating embeddings
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    text_content = f.read()
                meta = {}
                content = text_content
                if filepath.endswith((".md", ".txt")):
                    try:
                        post = frontmatter.loads(text_content)
                        content = post.content
                        meta = post.metadata or {}
                    except Exception:
                        pass
                folder = os.path.basename(os.path.dirname(filepath)) or "root"
                title = os.path.basename(filepath)
                category = category_override or meta.get("category", folder)
                tags = meta.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",")]
                elif not isinstance(tags, list):
                    tags = []
                chunks = chunk_markdown(content, max_chars=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
                headings = [c["heading"] for c in chunks if c.get("heading") and c["heading"] != "Root"]
                keywords = extract_keywords_from_text(content, title, headings, tags)
                summary_tuple = (filepath, title, folder, category, json.dumps(tags), json.dumps(list(set(headings))), json.dumps(keywords), mtime)
                return (filepath, mtime, [], "skipped", summary_tuple)
            except Exception:
                return (filepath, mtime, [], "skipped", None)
            
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
        headings = [c["heading"] for c in chunks if c.get("heading") and c["heading"] != "Root"]
        keywords = extract_keywords_from_text(content, title, headings, tags)

        points = []
        for idx, chunk in enumerate(chunks):
            chunk_content = chunk["content"].strip()
            if not chunk_content:
                continue
            
            point_id = get_chunk_uuid(filepath, idx)
            
            # Contextual embedding input
            contextual_embed_text = (
                f"Document Title: {title}\n"
                f"Folder: {folder}\n"
                f"Category: {category}\n"
                f"Section Heading: {chunk['heading']}\n"
                f"Tags: {', '.join(tags)}\n"
                f"Content:\n{chunk_content}"
            )
            vector = get_embedding(contextual_embed_text)
            
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

        summary_tuple = (
            filepath,
            title,
            folder,
            category,
            json.dumps(tags),
            json.dumps(list(set(headings))),
            json.dumps(keywords),
            mtime
        )
        return (filepath, mtime, points, "indexed", summary_tuple)
    except Exception as e:
        logger.error(f"Failed to process file {filepath}: {e}")
        return (filepath, None, [], "failed", None)

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
        summaries_to_update = [] # List of summary tuples
        files_to_delete_from_qdrant = [] # List of filepath

        # Process files concurrently in a thread pool (max 8 workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(process_file_worker, filepath, cat): filepath 
                for filepath, cat in found_files.items()
            }
            for future in concurrent.futures.as_completed(futures):
                filepath, mtime, points, status, summary_tuple = future.result()
                if summary_tuple:
                    summaries_to_update.append(summary_tuple)
                    
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
        try:
            with get_db_connection() as conn:
                if files_to_update_cache:
                    conn.executemany(
                        "INSERT OR REPLACE INTO indexed_files (filepath, mtime) VALUES (?, ?)",
                        files_to_update_cache
                    )
                if summaries_to_update:
                    conn.executemany(
                        """INSERT OR REPLACE INTO file_summaries 
                           (filepath, title, folder, category, tags, headings, keywords, mtime) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        summaries_to_update
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
                        conn.execute("DELETE FROM file_summaries WHERE filepath = ?", (cached_path,))
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
mcp_server = Server("notes-rag-mcp", version="1.0.1")

@mcp_server.list_tools()
async def list_tools() -> List[Tool]:
    dynamic_desc = get_dynamic_search_tool_description()
    return [
        Tool(
            name="search_notes",
            description=dynamic_desc,
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
            description="Force an immediate directory scan to index new/updated markdown & documentation files.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="index_status",
            description="Get indexing statistics of your documentation & notes vault.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]

@mcp_server.list_resources()
async def list_resources() -> List[Resource]:
    return [
        Resource(
            uri="notes://catalog/summary",
            name="Notes & Documentation Topic Catalog",
            description="Comprehensive catalog of indexed document titles, categories, tags, and extracted topics.",
            mimeType="text/markdown"
        )
    ]

@mcp_server.read_resource()
async def read_resource(uri: str) -> str:
    if uri == "notes://catalog/summary":
        try:
            with get_db_connection() as conn:
                rows = conn.execute("SELECT filepath, title, category, tags, keywords FROM file_summaries").fetchall()
            
            md = "# Notes & Documentation Topic Catalog\n\n"
            md += f"**Total Files Indexed:** {len(rows)}\n\n"
            md += "| Document Title | Category | Tags | Top Key Concepts |\n"
            md += "| --- | --- | --- | --- |\n"
            for r in rows:
                tags_str = ""
                kw_str = ""
                try:
                    tags_str = ", ".join(json.loads(r["tags"])) if r["tags"] else ""
                except Exception:
                    pass
                try:
                    kw_str = ", ".join(json.loads(r["keywords"])[:7]) if r["keywords"] else ""
                except Exception:
                    pass
                md += f"| {r['title']} | {r['category'] or '-'} | {tags_str or '-'} | {kw_str or '-'} |\n"
            return md
        except Exception as e:
            return f"Error building catalog resource: {str(e)}"
    raise ValueError(f"Unknown resource URI: {uri}")

@mcp_server.list_prompts()
async def list_prompts() -> List[Prompt]:
    return [
        Prompt(
            name="search_infrastructure_docs",
            description="Workflow to search system infrastructure documentation, container mappings, or network routes.",
            arguments=[
                PromptArgument(name="topic", description="The specific infrastructure topic to search for (e.g. ports, caddy, authelia)", required=True)
            ]
        )
    ]

@mcp_server.get_prompt()
async def get_prompt(name: str, arguments: dict = None) -> List[PromptMessage]:
    if name == "search_infrastructure_docs":
        topic = (arguments or {}).get("topic", "infrastructure")
        return [
            PromptMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"Please perform a search using the search_notes tool for topic '{topic}' and summarize the matching container mappings, port numbers, reverse proxy routes, or setup instructions."
                )
            )
        ]
    raise ValueError(f"Unknown prompt name: {name}")

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
            response = qdrant.query_points(
                collection_name=COLLECTION_NAME,
                query=query_vector,
                query_filter=query_filter,
                limit=limit
            )
            search_results = response.points

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
                f"Embedding Provider: {EMBEDDING_PROVIDER.upper()}\n"
                f"Embedding Model: {EMBEDDING_MODEL}"
            )
            return [TextContent(type="text", text=status_text)]
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to gather status: {str(e)}")]

    else:
        raise ValueError(f"Unknown tool name: {name}")

# FastAPI setup to support SSE
app = FastAPI(title="Notes RAG MCP Server")
sse_transport = SseServerTransport("/messages/")

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
            
            # Aggregate top keywords for UI display
            sum_rows = conn.execute("SELECT keywords FROM file_summaries").fetchall()
            kw_counts = {}
            for sr in sum_rows:
                if sr["keywords"]:
                    try:
                        kws = json.loads(sr["keywords"])
                        for kw in kws:
                            kw_counts[kw] = kw_counts.get(kw, 0) + 1
                    except Exception:
                        pass
            top_keywords = sorted(kw_counts.keys(), key=lambda k: kw_counts[k], reverse=True)[:25]
        
        points_count = 0
        if qdrant.collection_exists(COLLECTION_NAME):
            info = qdrant.get_collection(COLLECTION_NAME)
            points_count = info.points_count
            
        return {
            "paths_count": paths_count,
            "files_count": files_count,
            "points_count": points_count,
            "is_indexing": is_indexing,
            "last_indexed": last_indexed_time,
            "embedding_provider": EMBEDDING_PROVIDER.upper(),
            "embedding_model": EMBEDDING_MODEL,
            "top_keywords": top_keywords
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

app.mount("/messages", sse_transport.handle_post_message)

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

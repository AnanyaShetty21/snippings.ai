import sqlite3
import hashlib
import os
import numpy as np
from datetime import datetime

MODEL_NAME = "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"


class EmbeddingService:
    _instance = None
    _model = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(EmbeddingService, cls).__new__(cls)
        return cls._instance

    def __init__(self, cache_dir=None):
        if not hasattr(self, "_initialized"):
            self.cache_dir = cache_dir or os.environ.get("SNIPPINGS_CACHE_DIR", None)
            self._initialized = True

    def get_model(self):
        """Lazy-load the SentenceTransformer model once."""
        if EmbeddingService._model is None:
            from sentence_transformers import SentenceTransformer
            print(f"Loading embedding model: {MODEL_NAME}")
            kwargs = {}
            if self.cache_dir:
                kwargs["cache_folder"] = self.cache_dir
            EmbeddingService._model = SentenceTransformer(MODEL_NAME, **kwargs)
            print("Embedding model loaded successfully.")
        return EmbeddingService._model

    def encode_text(self, text: str) -> np.ndarray:
        """Generate normalized 384-d embedding vector for a string."""
        if not text:
            return np.zeros(384, dtype=np.float32)
        model = self.get_model()
        # normalize_embeddings=True ensures cosine similarity is just dot product
        emb = model.encode(text, normalize_embeddings=True, convert_to_numpy=True)
        return emb.astype(np.float32)

    def init_database(self, conn: sqlite3.Connection):
        """Create the embeddings table if it doesn't already exist."""
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snippet_embeddings (
                item_id INTEGER PRIMARY KEY,
                embedding BLOB NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at DATETIME
            )
        """)
        conn.commit()

    def _compute_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    def build_indexable_content(self, text=None, summary=None, caption=None) -> str:
        """Combine text, summary, and image caption into rich textual representation."""
        parts = []
        if text and text.strip():
            parts.append(text.strip())
        if summary and summary.strip() and summary.strip() != text.strip():
            parts.append(f"Summary: {summary.strip()}")
        if caption and caption.strip():
            parts.append(f"Image Caption: {caption.strip()}")
        return " | ".join(parts)

    def index_item(self, conn: sqlite3.Connection, item_id: int, text=None, summary=None, caption=None):
        """Index a single clipboard item if it is new or has changed."""
        content = self.build_indexable_content(text, summary, caption)
        if not content:
            return

        content_hash = self._compute_hash(content)
        cursor = conn.cursor()
        cursor.execute("SELECT content_hash FROM snippet_embeddings WHERE item_id = ?", (item_id,))
        row = cursor.fetchone()

        if row and row[0] == content_hash:
            # Already indexed and unchanged
            return

        emb = self.encode_text(content)
        emb_blob = emb.tobytes()

        cursor.execute("""
            INSERT OR REPLACE INTO snippet_embeddings (item_id, embedding, content_hash, updated_at)
            VALUES (?, ?, ?, ?)
        """, (item_id, emb_blob, content_hash, datetime.now()))
        conn.commit()

    def sync_all(self, db_name: str, progress_callback=None):
        """Ensure all items in clipboard_items are indexed."""
        conn = sqlite3.connect(db_name)
        self.init_database(conn)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, text, summary, caption
            FROM clipboard_items
            WHERE id NOT IN (SELECT item_id FROM snippet_embeddings)
               OR id IN (
                   SELECT c.id FROM clipboard_items c
                   JOIN snippet_embeddings e ON c.id = e.item_id
               )
        """)
        items = cursor.fetchall()
        total = len(items)

        indexed_count = 0
        for idx, (item_id, text, summary, caption) in enumerate(items):
            self.index_item(conn, item_id, text, summary, caption)
            indexed_count += 1
            if progress_callback:
                progress_callback(indexed_count, total)

        conn.close()
        return indexed_count

    def load_all_embeddings(self, conn: sqlite3.Connection):
        """
        Load all stored embeddings into memory as a dictionary:
        {item_id: np.ndarray(384, float32)}
        """
        cursor = conn.cursor()
        cursor.execute("SELECT item_id, embedding FROM snippet_embeddings")
        rows = cursor.fetchall()

        embeddings = {}
        for item_id, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            embeddings[item_id] = vec
        return embeddings

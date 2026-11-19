import sqlite3
import re
import numpy as np
from datetime import datetime, timedelta
from embedding_service import EmbeddingService
import credential_service


class RetrievalService:
    def __init__(self, db_name="smart_clipboard.db", embedding_service=None):
        self.db_name = db_name
        self.embedding_service = embedding_service or EmbeddingService()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self, conn: sqlite3.Connection = None):
        """Initialize FTS5 virtual table and synchronization triggers."""
        close_on_exit = False
        if conn is None:
            conn = self.get_connection()
            close_on_exit = True

        cursor = conn.cursor()

        # Create FTS5 virtual table
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS clipboard_fts USING fts5(
                id UNINDEXED,
                text,
                summary,
                caption,
                source,
                type1,
                type2,
                type3
            )
        """)

        # Triggers to keep FTS5 synchronized with clipboard_items
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_clipboard_insert AFTER INSERT ON clipboard_items
            BEGIN
                INSERT INTO clipboard_fts (id, text, summary, caption, source, type1, type2, type3)
                VALUES (new.id, new.text, new.summary, new.caption, new.source, new.type1, new.type2, new.type3);
            END;
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_clipboard_delete AFTER DELETE ON clipboard_items
            BEGIN
                DELETE FROM clipboard_fts WHERE id = old.id;
            END;
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_clipboard_update AFTER UPDATE ON clipboard_items
            BEGIN
                DELETE FROM clipboard_fts WHERE id = old.id;
                INSERT INTO clipboard_fts (id, text, summary, caption, source, type1, type2, type3)
                VALUES (new.id, new.text, new.summary, new.caption, new.source, new.type1, new.type2, new.type3);
            END;
        """)

        # Initial sync for any items not yet in FTS5
        cursor.execute("""
            INSERT INTO clipboard_fts (id, text, summary, caption, source, type1, type2, type3)
            SELECT id, text, summary, caption, source, type1, type2, type3
            FROM clipboard_items
            WHERE id NOT IN (SELECT id FROM clipboard_fts)
        """)

        conn.commit()

        # Also initialize embedding table
        self.embedding_service.init_database(conn)

        if close_on_exit:
            conn.close()

    def clean_fts_query(self, query: str) -> str:
        """Sanitize query into safe FTS5 query with OR combination."""
        # Find alphanumeric tokens and quoted phrases
        tokens = re.findall(r'"[^"]+"|[a-zA-Z0-9_]+', query)
        if not tokens:
            return ""
        # Quote individual tokens to prevent FTS5 syntax errors with special chars
        quoted = [t if t.startswith('"') else f'"{t}"' for t in tokens if len(t) > 1]
        if not quoted:
            return ""
        return " OR ".join(quoted)

    def search_fts(self, conn: sqlite3.Connection, query: str, limit: int = 50):
        """Execute FTS5 keyword search and return {item_id: rank}."""
        fts_query = self.clean_fts_query(query)
        if not fts_query:
            return {}

        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT id, bm25(clipboard_fts) AS rank
                FROM clipboard_fts
                WHERE clipboard_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit))
            results = {}
            for rank_idx, row in enumerate(cursor.fetchall()):
                results[row["id"]] = {
                    "rank": rank_idx + 1,
                    "bm25": row["rank"]
                }
            return results
        except Exception as e:
            print(f"FTS5 search error: {e}")
            return {}

    def search_semantic(self, conn: sqlite3.Connection, query: str, limit: int = 50):
        """Execute semantic search using embedding cosine similarity."""
        embeddings = self.embedding_service.load_all_embeddings(conn)
        if not embeddings:
            return {}

        query_vec = self.embedding_service.encode_text(query)

        item_ids = list(embeddings.keys())
        matrix = np.array([embeddings[i] for i in item_ids], dtype=np.float32)

        # Dot product of normalized vectors = Cosine similarity
        sims = np.dot(matrix, query_vec)

        # Pair up and sort
        scored = sorted(zip(item_ids, sims), key=lambda x: x[1], reverse=True)
        results = {}
        for rank_idx, (item_id, sim) in enumerate(scored[:limit]):
            results[item_id] = {
                "rank": rank_idx + 1,
                "similarity": float(sim)
            }
        return results

    def _matches_filters(self, item: dict, filters: dict) -> bool:
        """Check if item matches metadata filters (time range, type, source)."""
        if not filters:
            return True

        # Timestamp filter
        if "since" in filters and filters["since"]:
            item_time_str = item["timestamp"]
            if item_time_str:
                try:
                    # SQLite formats: 'YYYY-MM-DD HH:MM:SS.ffffff' or 'YYYY-MM-DD HH:MM:SS'
                    item_dt = datetime.fromisoformat(item_time_str.split(".")[0])
                    if item_dt < filters["since"]:
                        return False
                except Exception:
                    pass

        if "until" in filters and filters["until"]:
            item_time_str = item["timestamp"]
            if item_time_str:
                try:
                    item_dt = datetime.fromisoformat(item_time_str.split(".")[0])
                    if item_dt > filters["until"]:
                        return False
                except Exception:
                    pass

        # Content Type filter
        if "type" in filters and filters["type"]:
            target_type = filters["type"].lower()
            types = [t.lower() for t in [item["type1"], item["type2"], item["type3"]] if t]
            # Match directly or check if type substring is present
            if not any(target_type in t or t in target_type for t in types):
                return False

        return True

    def search_hybrid(self, query: str, metadata_filters: dict = None, top_k: int = 5,
                      semantic_weight: float = 0.6, fts_weight: float = 0.4, k_rrf: int = 60):
        """
        Execute Hybrid Semantic + Keyword Search using Reciprocal Rank Fusion (RRF).
        Returns a ranked list of matched clipboard item dictionaries.
        """
        conn = self.get_connection()
        self.init_database(conn)

        # 1. Fetch semantic results
        semantic_results = self.search_semantic(conn, query, limit=50)

        # 2. Fetch keyword FTS5 results
        fts_results = self.search_fts(conn, query, limit=50)

        # Combine all candidate IDs
        candidate_ids = set(semantic_results.keys()) | set(fts_results.keys())
        if not candidate_ids:
            conn.close()
            return []

        # 3. Calculate Reciprocal Rank Fusion score for each candidate
        scored_candidates = []
        for item_id in candidate_ids:
            sem_info = semantic_results.get(item_id)
            fts_info = fts_results.get(item_id)

            sem_rank = sem_info["rank"] if sem_info else 1000
            fts_rank = fts_info["rank"] if fts_info else 1000

            rrf_score = (semantic_weight / (k_rrf + sem_rank)) + (fts_weight / (k_rrf + fts_rank))
            sem_sim = sem_info["similarity"] if sem_info else 0.0

            scored_candidates.append({
                "id": item_id,
                "rrf_score": rrf_score,
                "semantic_sim": sem_sim,
                "fts_rank": fts_info["rank"] if fts_info else None
            })

        # Sort by RRF score descending
        scored_candidates.sort(key=lambda x: x["rrf_score"], reverse=True)

        # 4. Fetch full item details and apply metadata filters
        cursor = conn.cursor()
        final_results = []

        for cand in scored_candidates:
            cursor.execute("""
                SELECT id, text, summary, image, caption, type1, type2, type3, source, timestamp
                FROM clipboard_items
                WHERE id = ?
            """, (cand["id"],))
            row = cursor.fetchone()
            if not row:
                continue

            item = dict(row)

            # Apply metadata filters
            if not self._matches_filters(item, metadata_filters):
                continue

            # Format match percentage
            # RRF scores typically range from ~0.005 to ~0.02
            # We scale into a human-friendly match confidence percentage
            sim = cand["semantic_sim"]
            has_fts = cand["fts_rank"] is not None
            if has_fts and sim > 0:
                confidence = min(99, int(sim * 60 + 40))
            elif sim > 0:
                confidence = min(95, int(sim * 100))
            else:
                confidence = 70

            is_sens = credential_service.is_sensitive(
                item["text"],
                [item["type1"], item["type2"], item["type3"]]
            )

            item["match_confidence"] = confidence
            item["rrf_score"] = cand["rrf_score"]
            item["semantic_sim"] = cand["semantic_sim"]
            item["fts_match"] = has_fts
            item["is_sensitive"] = is_sens
            item["masked_text"] = credential_service.mask_snippet(item["text"]) if is_sens else item["text"]

            final_results.append(item)
            if len(final_results) >= top_k:
                break

        conn.close()
        return final_results

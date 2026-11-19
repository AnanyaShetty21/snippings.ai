import os
import sqlite3
import time
from datetime import datetime, timedelta
from embedding_service import EmbeddingService
from retrieval_service import RetrievalService
from query_service import QueryService
import credential_service

EVAL_DB = "eval_clipboard.db"

# Realistic test clipboard dataset
TEST_SNIPPETS = [
    {
        "id": 1,
        "text": "def binary_search(arr, target):\n    low, high = 0, len(arr) - 1\n    while low <= high:\n        mid = (low + high) // 2\n        if arr[mid] == target: return mid\n        elif arr[mid] < target: low = mid + 1\n        else: high = mid - 1\n    return -1",
        "summary": "Python implementation of iterative binary search algorithm.",
        "type1": "code", "type2": "algorithm", "type3": None,
        "source": "VS Code - binary_search.py",
        "timestamp": datetime.now() - timedelta(days=2)
    },
    {
        "id": 2,
        "text": "SELECT department, AVG(salary) as avg_sal, COUNT(*) as emp_count FROM employees WHERE status = 'ACTIVE' GROUP BY department HAVING AVG(salary) > 75000 ORDER BY avg_sal DESC;",
        "summary": "SQL query calculating average salary and employee count by department for active staff.",
        "type1": "code", "type2": "sql", "type3": "database",
        "source": "DBeaver - analytics.sql",
        "timestamp": datetime.now() - timedelta(days=5)
    },
    {
        "id": 3,
        "text": "https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task",
        "summary": "Official Python 3 documentation on asyncio tasks and event loops.",
        "type1": "url", "type2": "documentation", "type3": None,
        "source": "Google Chrome - Python Docs",
        "timestamp": datetime.now() - timedelta(days=1)
    },
    {
        "id": 4,
        "text": "TCP congestion control uses algorithms like Slow Start, Congestion Avoidance, Fast Retransmit, and Fast Recovery to adapt data transmission rates dynamically according to network congestion signals.",
        "summary": "Overview of TCP congestion control phases and rate adjustment mechanics.",
        "type1": "note", "type2": "networking", "type3": "academic",
        "source": "Obsidian - Computer Networks.md",
        "timestamp": datetime.now() - timedelta(days=7)
    },
    {
        "id": 5,
        "text": "Convolutional Neural Networks (CNNs) use kernel filters and spatial pooling to extract translational invariant features from visual imagery, while Recurrent Neural Networks (RNNs) preserve temporal hidden states.",
        "summary": "Comparison between Convolutional Neural Networks (CNN) and Recurrent Neural Networks (RNN).",
        "type1": "academic", "type2": "note", "type3": "ai",
        "source": "Notion - Deep Learning Notes",
        "timestamp": datetime.now() - timedelta(days=10)
    },
    {
        "id": 6,
        "text": "github_password = 'gh_token_x99A#SecretVault!2026'",
        "summary": "GitHub account password credential token.",
        "type1": "password", "type2": "credentials", "type3": None,
        "source": "1Password / Terminal",
        "timestamp": datetime.now() - timedelta(days=3)
    },
    {
        "id": 7,
        "text": "support-helpdesk@snippings.ai",
        "summary": "Customer support and developer assistance email contact.",
        "type1": "email", "type2": "business", "type3": None,
        "source": "Slack - #general",
        "timestamp": datetime.now() - timedelta(hours=4)
    },
    {
        "id": 8,
        "text": "office_wifi_password = 'GuestConnect$882!'",
        "summary": "Office guest Wi-Fi access passphrase.",
        "type1": "password", "type2": "credentials", "type3": None,
        "source": "Slack - #office",
        "timestamp": datetime.now() - timedelta(days=4)
    },
    {
        "id": 9,
        "text": "https://kubernetes.io/docs/concepts/services-networking/ingress/",
        "summary": "Kubernetes Ingress concepts, controllers, and routing documentation.",
        "type1": "url", "type2": "documentation", "type3": "cloud",
        "source": "Google Chrome - K8s Docs",
        "timestamp": datetime.now() - timedelta(days=12)
    },
    {
        "id": 10,
        "text": "Linear search scans each element sequentially in O(N) time complexity, whereas binary search requires a sorted array and operates in O(log N) time.",
        "summary": "Comparison between linear search and binary search algorithm complexities.",
        "type1": "note", "type2": "algorithm", "type3": "academic",
        "source": "Notes - Algorithms.txt",
        "timestamp": datetime.now() - timedelta(days=2)
    }
]

# Benchmark queries with ground truth target item ID
EVAL_QUERIES = [
    {
        "query": "Find the Python code I copied for binary search",
        "expected_id": 1
    },
    {
        "query": "Show me that SQL query involving employee salaries",
        "expected_id": 2
    },
    {
        "query": "What was the URL of the documentation I visited about asyncio?",
        "expected_id": 3
    },
    {
        "query": "Find the notes I copied about TCP congestion control",
        "expected_id": 4
    },
    {
        "query": "I remember copying something about neural networks last week. Can you find it?",
        "expected_id": 5
    },
    {
        "query": "Where is the GitHub password I copied?",
        "expected_id": 6
    },
    {
        "query": "What was the email address for support helpdesk?",
        "expected_id": 7
    },
    {
        "query": "Show me the Wi-Fi password",
        "expected_id": 8
    },
    {
        "query": "Kubernetes ingress networking documentation link",
        "expected_id": 9
    },
    {
        "query": "Differences in time complexity between search algorithms",
        "expected_id": 10
    }
]


def setup_eval_database(db_name: str):
    """Set up a fresh test database with benchmark snippets."""
    if os.path.exists(db_name):
        os.remove(db_name)

    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE clipboard_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            text TEXT,
            summary TEXT,
            image BLOB,
            caption TEXT,
            type1 TEXT,
            type2 TEXT,
            type3 TEXT,
            source TEXT NOT NULL,
            timestamp DATETIME
        )
    """)

    for s in TEST_SNIPPETS:
        cursor.execute("""
            INSERT INTO clipboard_items (id, text, summary, type1, type2, type3, source, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (s["id"], s["text"], s["summary"], s["type1"], s["type2"], s["type3"], s["source"], s["timestamp"]))

    conn.commit()
    conn.close()


def run_evaluation():
    print("=" * 70)
    print("   ASK MY CLIPBOARD — RETRIEVAL EVALUATION BENCHMARK")
    print("=" * 70)

    setup_eval_database(EVAL_DB)

    emb_service = EmbeddingService()
    retrieval_service = RetrievalService(EVAL_DB, emb_service)
    retrieval_service.init_database()

    print("\n[1/3] Indexing benchmark snippets into vector & FTS5 store...")
    t0 = time.time()
    indexed = emb_service.sync_all(EVAL_DB)
    print(f"Indexed {indexed} snippets in {time.time() - t0:.2f}s.")

    conn = retrieval_service.get_connection()

    # Track metrics for: Semantic-Only, FTS5-Only, Hybrid (Semantic + FTS5 RRF)
    modes = ["Semantic-Only", "FTS5-Only", "Hybrid (RRF)"]
    metrics = {
        m: {"top1": 0, "top3": 0, "top5": 0, "mrr": 0.0, "total": len(EVAL_QUERIES)}
        for m in modes
    }

    print("\n[2/3] Running benchmark queries across retrieval modalities...")
    print("-" * 70)

    for q_idx, item in enumerate(EVAL_QUERIES, 1):
        query = item["query"]
        expected_id = item["expected_id"]
        print(f"\nQuery {q_idx:2d}: \"{query}\" (Expected ID: {expected_id})")

        # 1. Semantic-Only
        sem_res = retrieval_service.search_semantic(conn, query, limit=10)
        sem_ranked = sorted(sem_res.keys(), key=lambda i: sem_res[i]["similarity"], reverse=True)

        # 2. FTS5-Only
        fts_res = retrieval_service.search_fts(conn, query, limit=10)
        fts_ranked = sorted(fts_res.keys(), key=lambda i: fts_res[i]["rank"])

        # 3. Hybrid RRF
        hybrid_res = retrieval_service.search_hybrid(query, top_k=10)
        hybrid_ranked = [r["id"] for r in hybrid_res]

        for mode, ranked in [("Semantic-Only", sem_ranked), ("FTS5-Only", fts_ranked), ("Hybrid (RRF)", hybrid_ranked)]:
            if expected_id in ranked:
                rank = ranked.index(expected_id) + 1
                if rank == 1:
                    metrics[mode]["top1"] += 1
                if rank <= 3:
                    metrics[mode]["top3"] += 1
                if rank <= 5:
                    metrics[mode]["top5"] += 1
                metrics[mode]["mrr"] += 1.0 / rank
            else:
                rank = None

            print(f"  {mode:<15}: Rank = {str(rank):<4} (Top 3: {str(ranked[:3])})")

    conn.close()

    print("\n[3/3] Evaluating Credential Protection & Masking...")
    # Verify that sensitive snippets are correctly detected and masked
    for s in TEST_SNIPPETS:
        is_sens = credential_service.is_sensitive(s["text"], [s["type1"], s["type2"], s["type3"]])
        if s["id"] in (6, 8):  # Passwords
            assert is_sens, f"Failed to detect credential in item {s['id']}"
            redacted = credential_service.redact_for_llm(s["text"])
            assert "gh_token" not in redacted and "GuestConnect" not in redacted, "Secret leaked in LLM prompt!"
            assert "[REDACTED_CREDENTIAL]" in redacted, "Redaction token missing!"
    print("  ✓ Sensitive credentials successfully detected and masked.")
    print("  ✓ Zero passwords or secrets leaked in LLM prompts.")

    print("\n" + "=" * 70)
    print(f"{'Retrieval Modality':<18} | {'Top-1 Acc':<10} | {'Top-3 Acc':<10} | {'Top-5 Acc':<10} | {'MRR':<8}")
    print("-" * 70)
    for m in modes:
        tot = metrics[m]["total"]
        top1_pct = (metrics[m]["top1"] / tot) * 100
        top3_pct = (metrics[m]["top3"] / tot) * 100
        top5_pct = (metrics[m]["top5"] / tot) * 100
        mrr = metrics[m]["mrr"] / tot
        print(f"{m:<18} | {top1_pct:>6.1f}%    | {top3_pct:>6.1f}%    | {top5_pct:>6.1f}%    | {mrr:>6.3f}")
    print("=" * 70)

    # Cleanup eval db
    if os.path.exists(EVAL_DB):
        os.remove(EVAL_DB)

    return metrics


if __name__ == "__main__":
    run_evaluation()

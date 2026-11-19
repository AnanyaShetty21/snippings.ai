import time
from retrieval_service import RetrievalService
from llm_service import LLMService
import credential_service


class QueryService:
    def __init__(self, db_name="smart_clipboard.db", embedding_service=None):
        self.retrieval_service = RetrievalService(db_name, embedding_service)
        self.llm_service = LLMService()

    def ask(self, query: str, use_llm: bool = True, top_k: int = 5) -> dict:
        """
        Process a natural language query over clipboard history:
        1. Extract metadata constraints (temporal, content types).
        2. Perform hybrid retrieval (semantic + FTS5 BM25).
        3. Optionally synthesize a grounded explanation via local Ollama.
        """
        start_time = time.time()
        query = query.strip()
        if not query:
            return {
                "query": "",
                "results": [],
                "answer": "Please enter a question or description of what you are looking for.",
                "constraints": {},
                "llm_used": False,
                "execution_time_ms": 0
            }

        # 1. Extract metadata constraints (dates, types)
        constraints = self.llm_service.extract_constraints_rule_based(query)

        # 2. Perform hybrid search
        results = self.retrieval_service.search_hybrid(
            query=query,
            metadata_filters=constraints,
            top_k=top_k
        )

        # If strict filtering yielded no results, fall back to unrestricted hybrid search
        if not results and constraints:
            fallback_results = self.retrieval_service.search_hybrid(
                query=query,
                metadata_filters=None,
                top_k=top_k
            )
            if fallback_results:
                results = fallback_results

        # 3. Optional grounded explanation via local Ollama
        answer = ""
        llm_used = False
        if use_llm and results:
            if self.llm_service.is_available():
                answer = self.llm_service.generate_grounded_answer(query, results)
                llm_used = bool(answer)

        # Default answer if no LLM was used or if LLM didn't return text
        if not answer:
            if results:
                count = len(results)
                top_conf = results[0]["match_confidence"]
                answer = f"Found {count} matching snippet{'s' if count > 1 else ''} in your clipboard history (top match: {top_conf}% confidence)."
            else:
                answer = "No matching clipboard snippets were found. Try broader keywords or check 'All Items'."

        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "query": query,
            "results": results,
            "answer": answer,
            "constraints": constraints,
            "llm_used": llm_used,
            "execution_time_ms": elapsed_ms
        }

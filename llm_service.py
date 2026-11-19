import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import credential_service


class LLMService:
    def __init__(self, endpoint="http://localhost:11434", model="qwen3:4b", timeout=12):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if local Ollama server is running and model is available."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                models = [m.get("name", "") for m in data.get("models", [])]
                # Check for model name match (e.g. 'qwen3:4b' or 'qwen3:4b-...')
                return any(self.model in m for m in models)
        except Exception:
            return False

    def extract_constraints_rule_based(self, query: str) -> dict:
        """
        Fast deterministic metadata extraction for temporal and content-type constraints.
        Works offline with zero latency.
        """
        q_lower = query.lower()
        now = datetime.now()
        filters = {}

        # Temporal constraints
        if "today" in q_lower:
            start_today = datetime(now.year, now.month, now.day)
            filters["since"] = start_today
        elif "yesterday" in q_lower:
            start_yesterday = datetime(now.year, now.month, now.day) - timedelta(days=1)
            end_yesterday = datetime(now.year, now.month, now.day)
            filters["since"] = start_yesterday
            filters["until"] = end_yesterday
        elif "last week" in q_lower or "past week" in q_lower:
            filters["since"] = now - timedelta(days=7)
        elif "this week" in q_lower:
            filters["since"] = now - timedelta(days=now.weekday())
        elif "recently" in q_lower:
            filters["since"] = now - timedelta(days=3)

        # Content type constraints
        if any(w in q_lower for w in ["code", "python", "sql", "function", "script", "java", "c++", "html"]):
            filters["type"] = "code"
        elif any(w in q_lower for w in ["password", "credentials", "api key", "secret", "token"]):
            filters["type"] = "password"
        elif any(w in q_lower for w in ["url", "link", "website", "documentation link", "github"]):
            filters["type"] = "url"
        elif any(w in q_lower for w in ["email", "e-mail"]):
            filters["type"] = "email"
        elif any(w in q_lower for w in ["note", "notes", "idea"]):
            filters["type"] = "note"

        return filters

    def _strip_thinking_tags(self, text: str) -> str:
        """Remove Qwen3/DeepSeek <think>...</think> reasoning blocks."""
        if not text:
            return ""
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
        return cleaned.strip()

    def generate_grounded_answer(self, query: str, retrieved_snippets: list) -> str:
        """
        Generate a concise, grounded explanation of the retrieved snippets.
        Strictly prevents hallucination and guarantees credential protection.
        """
        if not retrieved_snippets:
            return "No relevant clipboard snippets were found matching your query."

        # Redact credentials before constructing context
        snippet_contexts = []
        for idx, item in enumerate(retrieved_snippets[:3], 1):
            text_content = item.get("text") or item.get("caption") or "(Image snippet)"
            safe_text = credential_service.redact_for_llm(text_content)
            # Truncate overly long snippets in the prompt
            if len(safe_text) > 300:
                safe_text = safe_text[:300] + "..."
            timestamp = item.get("timestamp") or "Unknown date"
            source = item.get("source") or "Unknown app"
            snippet_contexts.append(
                f"[Snippet {idx}] (Copied: {timestamp} from {source}):\n{safe_text}"
            )

        context_str = "\n\n".join(snippet_contexts)

        prompt = f"""You are 'Ask My Clipboard', an AI assistant that answers questions based ONLY on the user's personal clipboard history.

User Question: "{query}"

Retrieved Clipboard Snippets:
{context_str}

Instructions:
1. Answer the user's question directly using ONLY the information present in the snippets above.
2. If the user asks where or when something was copied, cite the snippet number, timestamp, or source.
3. Be concise (2-4 sentences max).
4. NEVER invent or assume any information not present in the snippets.
5. If the snippets do not contain the answer, state clearly: "Based on your clipboard history, I could not find that information."
"""

        try:
            req_data = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": 200
                }
            }
            req = urllib.request.Request(
                f"{self.endpoint}/api/generate",
                data=json.dumps(req_data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_json = json.loads(resp.read().decode())
                raw_response = resp_json.get("response", "")
                cleaned = self._strip_thinking_tags(raw_response)
                return cleaned or raw_response.strip()
        except Exception as e:
            # Fallback gracefully
            return ""

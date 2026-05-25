"""Memory summarizer — compresses many memories into concise summaries using LLM."""

from __future__ import annotations

from typing import Any

from system.core.memory.schemas import MemoryEntry
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class MemorySummarizer:
    def __init__(self, llm_client: Any = None) -> None:
        self.llm_client = llm_client

    async def summarize_memories(self, memories: list[MemoryEntry]) -> str:
        if not memories:
            return "No memories to summarize."

        lines = [f"[{m.memory_type}] {m.title}: {m.content[:200]}" for m in memories[:20]]
        combined = "\n".join(lines)

        if self.llm_client:
            try:
                response = await self.llm_client.complete(
                    messages=[
                        {
                            "role": "user",
                            "content": f"Summarize these project memories concisely:\n{combined}",
                        }
                    ],
                    system="You are a technical project memory summarizer. Be concise and factual.",
                    max_tokens=512,
                    temperature=0.1,
                )
                return response.content
            except Exception as exc:
                logger.warning("LLM summarization failed", error=str(exc))

        return combined

    async def extract_key_facts(self, text: str, project_id: str) -> list[str]:
        if not self.llm_client:
            return [text[:200]] if text else []
        try:
            response = await self.llm_client.complete(
                messages=[
                    {
                        "role": "user",
                        "content": f"Extract 3-5 key technical facts from:\n{text}\n\nOutput as bullet points.",
                    }
                ],
                system="Extract only objective technical facts. Be brief.",
                max_tokens=256,
                temperature=0.0,
            )
            return [
                line.strip().lstrip("•-* ")
                for line in response.content.splitlines()
                if line.strip()
            ]
        except Exception as exc:
            logger.warning("Fact extraction failed", error=str(exc))
            return []

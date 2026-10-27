"""The agent's LLM: one interface, two backends, chosen with the AGENT_LLM setting.

- "muse-spark-1.3": Meta Model API (default)
- "muse-spark-open": Muse Spark Open, open weights served on Baseten

Every agent step that needs an LLM (fact extraction, photo judging, later dedupe and drafting)
goes through this interface, so the whole agent can run on open weights.
"""

from typing import Any, Literal, Protocol

AgentLLMName = Literal["muse-spark-1.3", "muse-spark-open"]


class AgentLLM(Protocol):
    model: str  # model id as logged in ModelCall

    async def chat_json(
        self,
        *,
        system: str,
        user_text: str,
        schema: dict[str, Any],
        schema_name: str,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...

"""One structured chat interface for agent steps and the independently configured pair judge."""

from typing import Any, Literal, Protocol

AgentLLMName = Literal[
    "muse-spark-1.3", "muse-spark-open", "muse-spark-open-fireworks", "muse-glimmer"
]


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

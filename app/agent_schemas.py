from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.tools import WorkoutProposalToolResult


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None

    message: Annotated[
        str,
        StringConstraints(
            strict=True, strip_whitespace=True, min_length=1, max_length=4000
        ),
    ]


class AgentAnswerResponse(BaseModel):
    conversation_id: UUID
    type: Literal["answer"] = "answer"
    message: str


class AgentProposalResponse(BaseModel):
    conversation_id: UUID
    type: Literal["proposal"] = "proposal"
    proposal: WorkoutProposalToolResult


AgentChatResponse = Annotated[
    AgentAnswerResponse | AgentProposalResponse, Field(discriminator="type")
]

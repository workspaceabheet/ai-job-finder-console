from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.auth import verify_request
from app.db import db_session
from app.deps import get_session_manager
from app.session_port import SessionManager

router = APIRouter(dependencies=[Depends(verify_request)])
CHAT_MAX_LENGTH = 200  # mirrored in static/js/chat.js -- change both together


class ChatIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _length(cls, v: str) -> str:
        if len(v) > CHAT_MAX_LENGTH:
            raise ValueError(f"direction too long (max {CHAT_MAX_LENGTH} characters)")
        return v


Session = Annotated[SessionManager, Depends(get_session_manager)]


@router.post("/chat")
async def set_chat_direction(body: ChatIn, session: Session):
    """Calls session.set_direction(body.text) — does NOT call execute_run or
    touch run_lock (AC8). Returns the new active ChatDirectionRecord.
    Over-length text -> 422 from the validator (reject with feedback)."""
    record = await session.set_direction(body.text)
    return asdict(record)


@router.get("/chat/history")
async def chat_history(session: Session):
    """Returns session.get_history() — list of ChatDirectionRecord, newest
    first, exactly one with active=true."""
    return [asdict(r) for r in await session.get_history()]


@router.get("/chat/status")
async def chat_status(session: Session):
    """Status line for the chat box (AC8/AC9/AC11 UI state), derived without
    widening the SessionManager contract:
      state="none"    -> no active direction (never set, or cleared by reset)
      state="pending" -> active direction not yet used by a completed run
      state="applied" -> the latest completed run started after the direction
                         was set and ran with exactly that direction."""
    active = next((r for r in await session.get_history() if r.active), None)
    if active is None:
        return {"state": "none", "text": None, "set_at": None}
    with db_session() as conn:
        last = conn.execute(
            "SELECT started_at, chat_direction FROM run_history "
            "WHERE status = 'complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    applied = (
        last is not None
        and last["chat_direction"] == active.text
        and last["started_at"] >= active.set_at
    )
    return {
        "state": "applied" if applied else "pending",
        "text": active.text,
        "set_at": active.set_at,
    }

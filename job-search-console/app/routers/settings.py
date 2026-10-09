from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import settings_store
from app.auth import verify_request
from app.db import db_session

router = APIRouter(dependencies=[Depends(verify_request)])


class SettingsOut(BaseModel):
    role_keywords: str
    seniority: str
    location: str
    must_haves: str
    dealbreakers: str
    notes: str
    saved_at: str | None
    is_default: bool


class SettingsIn(BaseModel):
    role_keywords: str = ""
    seniority: str = "Mid-level"
    location: str = ""
    must_haves: str = ""
    dealbreakers: str = ""
    notes: str = ""


def _to_out(profile: settings_store.SettingsProfile) -> SettingsOut:
    return SettingsOut(
        role_keywords=profile.role_keywords,
        seniority=profile.seniority,
        location=profile.location,
        must_haves=profile.must_haves,
        dealbreakers=profile.dealbreakers,
        notes=profile.notes,
        saved_at=profile.saved_at,
        is_default=profile.is_default,
    )


@router.get("/settings", response_model=SettingsOut)
async def read_settings() -> SettingsOut:
    with db_session() as conn:
        return _to_out(settings_store.get_settings(conn))


@router.post("/settings", response_model=SettingsOut)
async def write_settings(body: SettingsIn) -> SettingsOut:
    with db_session() as conn:
        profile = settings_store.save_settings(
            conn,
            role_keywords=body.role_keywords,
            seniority=body.seniority,
            location=body.location,
            must_haves=body.must_haves,
            dealbreakers=body.dealbreakers,
            notes=body.notes,
        )
    return _to_out(profile)

"""Accessors for the agent-port instances wired in app/main.py.

Routers depend on these (never on a concrete class), so swapping a fake for a
real implementation in main.py needs no router changes."""

from fastapi import Request

from app.ports import ScoringPort, SourcingPort
from app.session_port import SessionManager


def get_sourcing_port(request: Request) -> SourcingPort:
    return request.app.state.sourcing_port


def get_scoring_port(request: Request) -> ScoringPort:
    return request.app.state.scoring_port


def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager

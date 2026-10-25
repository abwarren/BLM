"""The BLM assistant — a read-only Q&A agent over the platform.

Public surface:
    from blm_v4.assistant.agent import answer, chat, credentials, available
    from blm_v4.assistant.tools import run_tool, tool_schemas
    from blm_v4.assistant.api import router          # /api/v4/assistant/*

Read-only by construction: see tools.py.
"""

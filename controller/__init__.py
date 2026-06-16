"""clawd-controller — the AI project-manager layer over the fleet.

A relay/harness *client* (never imports server.py — same boundary as fleet/) that
materializes a semantic world-model of every session, keeps a task ledger of
intent, and exposes both as MCP so any agent can be the PM brain.

See docs/CONTROLLER.md for the design.
"""

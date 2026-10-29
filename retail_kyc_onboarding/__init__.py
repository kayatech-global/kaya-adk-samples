"""Retail KYC Onboarding, a Google ADK agent exported from Kaya. ADK finds it through `agent.root_agent` and `agent.app`."""

import os

# This agent relies on ADK features ADK 2.12 still marks experimental (resumable apps for pauses, its server's credential
# service), and ADK warns on every use. Its own switch silences them; a value already set in the environment wins.
os.environ.setdefault("ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS", "true")

from . import agent  # noqa: E402, F401  (ADK discovery: `adk run` and `adk web` read agent.root_agent)

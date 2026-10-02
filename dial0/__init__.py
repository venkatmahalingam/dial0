"""Dial 0 for SONiC: talk to a SONiC switch in plain English, on the switch itself.

A small local model (llama.cpp) plus an agent harness: request routing (most requests need no model), planning,
guardrails (only commands from the operator's reference, checked against the switch's own CLI, values from the
request, y/N before changes), execution with error recovery, working memory, workflows (health, security, CVE) and
blueprints (template-based configuration). See docs/ARCHITECTURE.md."""

__version__ = "0.9.0"

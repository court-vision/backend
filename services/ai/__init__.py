"""
The AI layer: a model that answers questions by consuming Court Vision's own
services, never by computing from a context dump (docs/AI_LAYER_PLAN.md).

    guards.py   kill switch and daily quotas, checked before any model call
    tools.py    the model-visible surface: thin wrappers over services/
    prompts.py  the system prompt, a byte-stable constant
    client.py   the Anthropic client
    service.py  the bounded tool loop behind POST /v1/internal/ai/ask
"""

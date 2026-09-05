"""Chat provider adapters.

Only `fake` is importable without extras. The SDK-backed adapters are NOT re-exported
here on purpose: `from .anthropic_adapter import AnthropicChatProvider` in this file
would run at `import wozto_ai_reference.llm_gateway` time and drag the optional
dependency question into every import of the package. Import them by module path.
"""

from .fake import Answer, Fail, PartialThenFail, ScriptedProvider, ScriptExhausted

__all__ = [
    "Answer",
    "Fail",
    "PartialThenFail",
    "ScriptExhausted",
    "ScriptedProvider",
]

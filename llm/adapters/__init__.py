from .anthropic import AnthropicAdapter
from .chat_completions import ChatCompletionsAdapter
from .gemini import GeminiAdapter
from .openai import OpenAIResponsesAdapter

ADAPTERS = {
    "chat_completions": ChatCompletionsAdapter,
    "responses": OpenAIResponsesAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
}

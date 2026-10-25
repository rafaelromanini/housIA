"""Erro do LLM com mensagem amigável para a interface (separado para os provedores não importarem o client)."""


class LLMError(RuntimeError):
    """Erro com mensagem amigável para exibir na interface."""

"""Interactive model selection, private persistence, and live client replacement."""

import getpass
import os
import sys
import warnings
from dataclasses import dataclass, field, replace

from llm import ConfigurationError, LLMClient
from llm.model_catalog import require_supported_model, supported_providers
from llm.providers import get_provider

from .config import read_config, save_user_config
from .installation import user_config_path
from .model_picker import pick_model

RESET_SETTINGS = {
    "LLM_THINKING": "auto",
    "LLM_REASONING_EFFORT": "",
    "LLM_THINKING_BUDGET": "",
    "LLM_THINKING_HISTORY": "auto",
    "LLM_THINKING_PROFILE": "{}",
    "LLM_EXTRA_JSON": "{}",
    "LLM_CONTEXT_WINDOW": "",
}
SWITCH_NOTICE = "模型已切换并保存到用户配置；上下文已清空，文件修改保留。"


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None


def clean_value(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("请输入单行文本，不能包含控制字符")
    return value.strip()


class ModelWizard:
    def __init__(self, provider, model, *, api_key=None, base_url=None):
        try:
            provider = get_provider(provider).name
        except ConfigurationError:
            provider = "deepseek"
        self.original_provider = provider
        self.original_model = model or ""
        self.original_key = api_key
        self.base_url = base_url
        path = user_config_path()
        self.saved = read_config(path) if path.exists() else {}
        self.provider = provider if provider in supported_providers() else supported_providers()[0]
        self.model = self.original_model
        self.stage = "provider"

    @property
    def secret(self):
        return self.stage == "key"

    def existing_key(self):
        if self.provider == self.original_provider and self.original_key:
            return self.original_key
        key = get_provider(self.provider).api_key_env
        return self.saved.get(key) or os.environ.get(key) or ""

    def prompt(self):
        if self.stage == "provider":
            choices = "\n".join(
                f"  {index}. {name}" for index, name in enumerate(supported_providers(), 1)
            )
            return f"选择供应商（序号或名称，回车保留 {self.provider}）：\n{choices}\n供应商> "
        if self.stage == "model":
            return "搜索模型；↑↓ / 滚轮选择，PgUp/PgDn 翻页，Enter 确认，Ctrl+C 取消。"
        reuse = "，回车保留已配置的 Key" if self.existing_key() else ""
        return f"API Key（输入隐藏{reuse}）> "

    def submit(self, value):
        value = clean_value(value)
        if self.stage == "provider":
            if value.isdigit():
                number = int(value)
                if not 1 <= number <= len(supported_providers()):
                    raise ValueError("请选择列表中的供应商序号")
                value = supported_providers()[number - 1]
            try:
                provider = get_provider(value or self.provider).name
            except ConfigurationError:
                raise ValueError("请选择列表中的供应商") from None
            if provider not in supported_providers():
                raise ValueError("该供应商尚无已支持的模型，请选择列表中的供应商")
            self.provider = provider
            self.model = self.original_model if provider == self.original_provider else ""
            self.stage = "model"
        elif self.stage == "model":
            self.model = require_supported_model(self.provider, value or self.model).id
            self.stage = "key"
        else:
            key = value or self.existing_key()
            if not key:
                raise ValueError("API Key 不能为空")
            return ModelSelection(
                self.provider,
                self.model,
                key,
                self.base_url if self.provider == self.original_provider else None,
            )
        return None


def prompt_model(wizard: ModelWizard) -> ModelSelection:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("模型设置需要交互终端；也可直接编辑用户配置文件")
    print("设置模型；Ctrl+C 取消。供应商切换使用新供应商的默认 API 地址。")
    while True:
        try:
            if wizard.secret:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    value = getpass.getpass(wizard.prompt())
            elif wizard.stage == "model":
                value = pick_model(wizard.provider, wizard.model)
            else:
                value = input(wizard.prompt())
            selection = wizard.submit(value)
            if selection is not None:
                return selection
        except getpass.GetPassWarning:
            raise ValueError("当前终端无法隐藏 API Key 输入，请使用交互终端") from None
        except ValueError as error:
            print(str(error))


def persist_selection(selection, *, reset):
    require_supported_model(selection.provider, selection.model)
    values = {
        "LLM_PROVIDER": selection.provider,
        "LLM_MODEL": selection.model,
        "LLM_BASE_URL": selection.base_url or "",
        get_provider(selection.provider).api_key_env: selection.api_key,
    }
    if reset:
        values.update(RESET_SETTINGS)
    return save_user_config(values)


class ModelControl:
    def __init__(
        self, runtime, config, *, thinking=None, status=None, tracer=None, client_factory=LLMClient
    ):
        self.runtime = runtime
        self.config = config
        self.initial_client = runtime.llm
        self.thinking = thinking
        self.status = status
        self.tracer = tracer
        self.client_factory = client_factory

    def describe(self):
        return (
            f"模型：{get_provider(self.config.provider).name} / {self.config.model}（/model 切换）"
        )

    def wizard(self):
        return ModelWizard(
            self.config.provider,
            self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )

    def switch(self, selection):
        require_supported_model(selection.provider, selection.model)
        new_config = replace(
            self.config,
            provider=selection.provider,
            model=selection.model,
            api_key=selection.api_key,
            base_url=selection.base_url,
        )
        client = self.client_factory(new_config)
        prepared = None
        try:
            if self.thinking and hasattr(self.thinking, "prepare_model"):
                prepared = self.thinking.prepare_model(selection, client)
            if self.status:
                lookup = getattr(client, "get_context_limit", None)
                if lookup:
                    lookup()
            persist_selection(selection, reset=True)
        except BaseException:
            client.close()
            raise
        old = self.runtime.llm
        self.runtime.llm = client
        self.config = new_config
        self.runtime.request_extra = prepared[1] if prepared else {}
        self.runtime.thinking_settings = {"mode": "auto", "effort": None, "budget": None}
        if prepared:
            self.thinking.adopt(prepared[0])
        elif self.thinking:
            self.thinking.provider = selection.provider
            self.thinking.model = selection.model
            self.thinking.base = {}
            self.thinking.current = dict(self.runtime.thinking_settings)
        if self.status:
            self.status.bind_context_model(client, reset=True)
            self.status.reset_context()
        if self.tracer:
            self.tracer.set_model(selection.provider, selection.model)
        close = getattr(old, "close", None)
        if close:
            close()
        return SWITCH_NOTICE + "\n" + self.describe()

    def close(self):
        if self.runtime.llm is not self.initial_client:
            self.runtime.llm.close()

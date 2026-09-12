from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core.provider.entities import ProviderType
from astrbot.core.utils.error_redaction import safe_error

if TYPE_CHECKING:
    from astrbot.core.provider.provider import Provider


MODEL_LIST_CACHE_TTL_SECONDS_DEFAULT = 30.0
MODEL_LOOKUP_MAX_CONCURRENCY_DEFAULT = 4
MODEL_LOOKUP_MAX_CONCURRENCY_UPPER_BOUND = 16
MODEL_LIST_CACHE_TTL_KEY = "model_list_cache_ttl_seconds"
MODEL_LOOKUP_MAX_CONCURRENCY_KEY = "model_lookup_max_concurrency"
MODEL_CACHE_MAX_ENTRIES = 512


@dataclass(frozen=True)
class _ModelLookupConfig:
    umo: str | None
    cache_ttl_seconds: float
    max_concurrency: int


class _ModelCache:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str | None], tuple[float, list[str]]] = {}

    def get(self, provider_id: str, umo: str | None, ttl: float) -> list[str] | None:
        if ttl <= 0:
            return None
        entry = self._store.get((provider_id, umo))
        if not entry:
            return None
        timestamp, models = entry
        if time.monotonic() - timestamp > ttl:
            self._store.pop((provider_id, umo), None)
            return None
        return models

    def set(
        self, provider_id: str, umo: str | None, models: list[str], ttl: float
    ) -> None:
        if ttl <= 0:
            return
        self._store[(provider_id, umo)] = (time.monotonic(), list(models))
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self._store) <= MODEL_CACHE_MAX_ENTRIES:
            return
        # Drop oldest entries first when cache grows too large.
        overflow = len(self._store) - MODEL_CACHE_MAX_ENTRIES
        for key, _ in sorted(
            self._store.items(),
            key=lambda item: item[1][0],
        )[:overflow]:
            self._store.pop(key, None)

    def invalidate(
        self, provider_id: str | None = None, *, umo: str | None = None
    ) -> None:
        if provider_id is None:
            self._store.clear()
            return
        if umo is not None:
            self._store.pop((provider_id, umo), None)
            return
        stale_keys = [
            cache_key for cache_key in self._store if cache_key[0] == provider_id
        ]
        for cache_key in stale_keys:
            self._store.pop(cache_key, None)


class ProviderCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._model_cache = _ModelCache()
        self._register_provider_change_hook()

    def _register_provider_change_hook(self) -> None:
        set_change_callback = getattr(
            self.context.provider_manager,
            "set_provider_change_callback",
            None,
        )
        if callable(set_change_callback):
            set_change_callback(self._on_provider_manager_changed)
            return
        register_change_hook = getattr(
            self.context.provider_manager,
            "register_provider_change_hook",
            None,
        )
        if callable(register_change_hook):
            register_change_hook(self._on_provider_manager_changed)

    def invalidate_provider_models_cache(
        self, provider_id: str | None = None, *, umo: str | None = None
    ) -> None:
        """Public hook for cache invalidation on external provider config changes."""
        self._model_cache.invalidate(provider_id, umo=umo)

    def _on_provider_manager_changed(
        self,
        provider_id: str,
        provider_type: ProviderType,
        umo: str | None,
    ) -> None:
        if provider_type == ProviderType.CHAT_COMPLETION:
            self.invalidate_provider_models_cache(provider_id, umo=umo)

    def _get_provider_settings(self, umo: str | None) -> dict:
        if not umo:
            return {}
        try:
            return self.context.get_config(umo).get("provider_settings", {}) or {}
        except Exception as e:
            logger.debug(
                "读取 provider_settings 失败，使用默认值: %s",
                safe_error("", e),
            )
            return {}

    def _get_model_cache_ttl(self, umo: str | None) -> float:
        settings = self._get_provider_settings(umo)
        raw = settings.get(
            MODEL_LIST_CACHE_TTL_KEY,
            MODEL_LIST_CACHE_TTL_SECONDS_DEFAULT,
        )
        try:
            return max(float(raw), 0.0)
        except Exception as e:
            logger.debug(
                "读取 %s 失败，回退默认值 %r: %s",
                MODEL_LIST_CACHE_TTL_KEY,
                MODEL_LIST_CACHE_TTL_SECONDS_DEFAULT,
                safe_error("", e),
            )
            return MODEL_LIST_CACHE_TTL_SECONDS_DEFAULT

    def _get_model_lookup_concurrency(self, umo: str | None) -> int:
        settings = self._get_provider_settings(umo)
        raw = settings.get(
            MODEL_LOOKUP_MAX_CONCURRENCY_KEY,
            MODEL_LOOKUP_MAX_CONCURRENCY_DEFAULT,
        )
        try:
            value = int(raw)
        except Exception as e:
            logger.debug(
                "读取 %s 失败，回退默认值 %r: %s",
                MODEL_LOOKUP_MAX_CONCURRENCY_KEY,
                MODEL_LOOKUP_MAX_CONCURRENCY_DEFAULT,
                safe_error("", e),
            )
            value = MODEL_LOOKUP_MAX_CONCURRENCY_DEFAULT
        return min(max(value, 1), MODEL_LOOKUP_MAX_CONCURRENCY_UPPER_BOUND)

    def _get_model_lookup_config(self, umo: str | None) -> _ModelLookupConfig:
        return _ModelLookupConfig(
            umo=umo,
            cache_ttl_seconds=self._get_model_cache_ttl(umo),
            max_concurrency=self._get_model_lookup_concurrency(umo),
        )

    def _resolve_model_name(
        self,
        model_name: str,
        models: Sequence[str],
    ) -> str | None:
        """Resolve model name with precedence:
        exact > case-insensitive > provider-qualified suffix.
        """
        requested = model_name.strip()
        if not requested:
            return None

        requested_norm = requested.casefold()

        # exact / case-insensitive match
        for candidate in models:
            if candidate == requested or candidate.casefold() == requested_norm:
                return candidate

        # provider-qualified suffix match:
        # e.g. candidate `openai/gpt-4o` should match requested `gpt-4o`.
        for candidate in models:
            cand_norm = candidate.casefold()
            if cand_norm.endswith(f"/{requested_norm}") or cand_norm.endswith(
                f":{requested_norm}"
            ):
                return candidate

        return None

    def _apply_model(
        self, prov: Provider, model_name: str, *, umo: str | None = None
    ) -> str:
        prov.set_model(model_name)
        self.invalidate_provider_models_cache(prov.meta().id, umo=umo)
        return f"切换模型成功。当前提供商: [{prov.meta().id}] 当前模型: [{prov.get_model()}]"

    async def _get_provider_models(
        self,
        provider: Provider,
        *,
        config: _ModelLookupConfig,
        use_cache: bool = True,
    ) -> list[str]:
        provider_id = provider.meta().id
        ttl_seconds = config.cache_ttl_seconds
        umo = config.umo
        if use_cache:
            cached = self._model_cache.get(provider_id, umo, ttl_seconds)
            if cached is not None:
                return cached

        models = list(await provider.get_models())
        if use_cache:
            self._model_cache.set(provider_id, umo, models, ttl_seconds)
        return models

    async def _get_models_or_reply_error(
        self,
        message: AstrMessageEvent,
        prov: Provider,
        config: _ModelLookupConfig,
        *,
        error_prefix: str,
        disable_t2i: bool = False,
        warning_log: str | None = None,
    ) -> list[str] | None:
        try:
            return await self._get_provider_models(prov, config=config)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if warning_log is not None:
                logger.warning(
                    warning_log,
                    prov.meta().id,
                    safe_error("", e),
                )
            result = MessageEventResult().message(safe_error(error_prefix, e))
            if disable_t2i:
                result = result.use_t2i(False)
            message.set_result(result)
            return None

    async def _find_provider_for_model(
        self,
        model_name: str,
        *,
        exclude_provider_id: str | None = None,
        config: _ModelLookupConfig,
        use_cache: bool = True,
    ) -> tuple[Provider | None, str | None]:
        all_providers = []
        for provider in self.context.get_all_providers():
            provider_meta = provider.meta()
            if provider_meta.provider_type != ProviderType.CHAT_COMPLETION:
                continue
            if (
                exclude_provider_id is not None
                and provider_meta.id == exclude_provider_id
            ):
                continue
            all_providers.append(provider)
        if not all_providers:
            return None, None

        semaphore = asyncio.Semaphore(config.max_concurrency)

        async def fetch_models(
            provider: Provider,
        ) -> tuple[Provider, list[str] | None, str | None]:
            async with semaphore:
                try:
                    models = await self._get_provider_models(
                        provider,
                        config=config,
                        use_cache=use_cache,
                    )
                    return provider, models, None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    err = safe_error("", e)
                    logger.debug(
                        "跨提供商查找模型 %s 获取 %s 模型列表失败: %s",
                        model_name,
                        provider.meta().id,
                        err,
                    )
                    return provider, None, err

        results = await asyncio.gather(
            *(fetch_models(provider) for provider in all_providers)
        )
        failed_provider_errors: list[tuple[str, str]] = []
        for provider, models, err in results:
            if err is not None:
                failed_provider_errors.append((provider.meta().id, err))
                continue
            if models is None:
                continue

            matched_model_name = self._resolve_model_name(model_name, models)
            if matched_model_name is not None:
                return provider, matched_model_name

        if failed_provider_errors and len(failed_provider_errors) == len(all_providers):
            failed_ids = ",".join(
                provider_id for provider_id, _ in failed_provider_errors
            )
            logger.error(
                "跨提供商查找模型 %s 时，所有 %d 个提供商的 get_models() 均失败: %s。请检查配置或网络",
                model_name,
                len(all_providers),
                failed_ids,
            )
        elif failed_provider_errors:
            logger.debug(
                "跨提供商查找模型 %s 时有 %d 个提供商获取模型失败: %s",
                model_name,
                len(failed_provider_errors),
                ",".join(
                    f"{provider_id}({error})"
                    for provider_id, error in failed_provider_errors
                ),
            )
        return None, None

    async def _switch_model_by_name(
        self, message: AstrMessageEvent, model_name: str, prov: Provider
    ) -> None:
        model_name = model_name.strip()
        if not model_name:
            message.set_result(MessageEventResult().message("模型名不能为空。"))
            return

        umo = message.unified_msg_origin
        config = self._get_model_lookup_config(umo)
        curr_provider_id = prov.meta().id

        models = await self._get_models_or_reply_error(
            message,
            prov,
            config,
            error_prefix="获取当前提供商模型列表失败: ",
            warning_log="获取当前提供商 %s 模型列表失败，停止跨提供商查找: %s",
        )
        if models is None:
            return

        matched_model_name = self._resolve_model_name(model_name, models)
        if matched_model_name is not None:
            message.set_result(
                MessageEventResult().message(
                    self._apply_model(prov, matched_model_name, umo=umo)
                ),
            )
            return

        target_prov, matched_target_model_name = await self._find_provider_for_model(
            model_name,
            exclude_provider_id=curr_provider_id,
            config=config,
        )

        if target_prov is None or matched_target_model_name is None:
            message.set_result(
                MessageEventResult().message(
                    f"模型 [{model_name}] 未在任何已配置的提供商中找到，或所有提供商模型列表获取失败，请检查配置或网络后重试。",
                ),
            )
            return

        target_id = target_prov.meta().id
        try:
            await self.context.provider_manager.set_provider(
                provider_id=target_id,
                provider_type=ProviderType.CHAT_COMPLETION,
                umo=umo,
            )
            self._apply_model(target_prov, matched_target_model_name, umo=umo)
            message.set_result(
                MessageEventResult().message(
                    f"检测到模型 [{matched_target_model_name}] 属于提供商 [{target_id}]，已自动切换提供商并设置模型。",
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            message.set_result(
                MessageEventResult().message(
                    safe_error("跨提供商切换并设置模型失败: ", e)
                ),
            )

    async def model_ls(
        self,
        message: AstrMessageEvent,
        idx_or_name: int | str | None = None,
    ) -> None:
        """查看或者切换模型"""
        prov = self.context.get_using_provider(message.unified_msg_origin)
        if not prov:
            message.set_result(
                MessageEventResult().message("未找到任何 LLM 提供商。请先配置。"),
            )
            return
        config = self._get_model_lookup_config(message.unified_msg_origin)

        if idx_or_name is None:
            models = await self._get_models_or_reply_error(
                message,
                prov,
                config,
                error_prefix="获取模型列表失败: ",
                disable_t2i=True,
            )
            if models is None:
                return
            parts = ["下面列出了此模型提供商可用模型:"]
            for i, model in enumerate(models, 1):
                parts.append(f"\n{i}. {model}")

            curr_model = prov.get_model() or "无"
            parts.append(f"\n当前模型: [{curr_model}]")
            parts.append(
                "\nTips: 使用 /model <模型名/编号> 切换模型。输入模型名时可自动跨提供商查找并切换。"
            )

            ret = "".join(parts)
            message.set_result(MessageEventResult().message(ret).use_t2i(False))
        elif isinstance(idx_or_name, int):
            models = await self._get_models_or_reply_error(
                message,
                prov,
                config,
                error_prefix="获取模型列表失败: ",
            )
            if models is None:
                return
            if idx_or_name > len(models) or idx_or_name < 1:
                message.set_result(MessageEventResult().message("模型序号错误。"))
            else:
                try:
                    new_model = models[idx_or_name - 1]
                    message.set_result(
                        MessageEventResult().message(
                            self._apply_model(
                                prov,
                                new_model,
                                umo=message.unified_msg_origin,
                            )
                        ),
                    )
                except Exception as e:
                    message.set_result(
                        MessageEventResult().message(
                            safe_error("切换模型未知错误: ", e)
                        ),
                    )
                    return
        else:
            await self._switch_model_by_name(message, idx_or_name, prov)

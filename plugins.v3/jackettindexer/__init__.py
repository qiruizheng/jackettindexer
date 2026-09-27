# -*- coding: utf-8 -*-
"""
Jackett 索引器插件（MoviePilot v3）

把 Jackett 作为一个「站点」接入 MoviePilot 的统一搜索：
- 通过 get_module() 把 search_torrents 注册为插件资源搜索源；
- MoviePilot 在搜索影视时，会调用 search_plugin_torrents 合并所有插件结果，
  本插件随即查询 Jackett 的 torznab API（默认聚合全部索引器 /api/v2.0/indexers/all/...），
  把返回的种子以 TorrentInfo 形式回灌进搜索结果，站点名显示为「Jackett」。

仓库结构（MoviePilot 插件市场格式）：
  package.v3.json                      <- 插件索引（市场清单），键/id 与目录名、类名保持一致
  plugins.v3/jackettindexer/__init__.py <- 插件源码

注意：MoviePilot 运行时以「类名」作为 running_plugins 的 key，而市场/URL 以「目录名/package id」
作为 plugin_id；二者必须完全一致，否则 /api/v1/plugin/form 会返回 404（配置加载失败）。
本插件目录名、类名、package id 统一为 jackettindexer。

使用方式：
  1. 在 MoviePilot 后台「系统设置 -> 插件 -> 插件市场」追加本仓库地址
     https://github.com/<你的用户名>/<本仓库名>
  2. 刷新市场，找到「Jackett 索引器」并安装；
  3. 启用插件，在配置中填写 Jackett 地址与 API Key（默认已填局域网地址）；
  4. 搜索影视时，结果中会出现 site=Jackett 的条目。
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

# 导入路径对齐官方 JackettExtend（已在 MoviePilot v3 容器内验证可用），
# 确保 TorrentInfo 真实导入成功，否则搜索会静默返回空。
try:
    from app.plugins import _PluginBase
    from app.core.context import TorrentInfo
    from app.schemas import MediaType
    from app.log import logger
except Exception:  # pragma: no cover - 独立运行兜底
    _PluginBase = object  # type: ignore
    TorrentInfo = None  # type: ignore
    MediaType = None  # type: ignore
    logger = logging.getLogger("jackettindexer")  # type: ignore


_TORZNAB_NS = {"torznab": "http://torznab.com/schemas/2015/feed"}


class jackettindexer(_PluginBase):
    # ---- 插件元信息（MoviePilot 后台展示用）----
    plugin_name = "Jackett 索引器"
    plugin_desc = "将 Jackett 作为站点接入 MoviePilot 搜索，返回全部索引器聚合结果。"
    plugin_version = "1.0.0"
    plugin_author = "Qiruizheng"
    author_url = ""

    # ---- 默认配置 ----
    _default_config = {
        "enabled": True,
        "jackett_url": "http://192.168.2.220:9117",
        "api_key": "irzy7mdb318o91wrwai1q9p91cdvvivr",
        "site_name": "Jackett",
        "indexer": "all",
        "timeout": 30,
        "filter_by_type": False,
        "proxy": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._config: Dict[str, Any] = dict(self._default_config)

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = dict(self._default_config)
        if config:
            self._config.update(config)
        self._enabled = bool(self._config.get("enabled"))
        logger.info("Jackett 索引器插件初始化完成，启用状态=%s", self._enabled)

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self):
        # 顶层用单个 VForm 根组件包裹，符合 MoviePilot v3 前端渲染契约
        return (
            [
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {"label": "启用 Jackett 搜索", "model": "enabled"},
                                            "id": "enabled",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {"label": "按媒体类型过滤分类", "model": "filter_by_type"},
                                            "id": "filter_by_type",
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "label": "Jackett 地址",
                                                "model": "jackett_url",
                                                "placeholder": "http://192.168.2.220:9117",
                                            },
                                            "id": "jackett_url",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {"label": "API Key", "model": "api_key"},
                                            "id": "api_key",
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 4},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "label": "站点名称",
                                                "model": "site_name",
                                                "placeholder": "Jackett",
                                            },
                                            "id": "site_name",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 4},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "label": "索引器ID (all=全部)",
                                                "model": "indexer",
                                                "placeholder": "all",
                                            },
                                            "id": "indexer",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 4},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "label": "超时(秒)",
                                                "model": "timeout",
                                                "type": "number",
                                            },
                                            "id": "timeout",
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "label": "走代理下载（Jackett 在局域网请保持关闭）",
                                                "model": "proxy",
                                            },
                                            "id": "proxy",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
            self._default_config,
        )

    def get_page(self):
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            f"Jackett 索引器当前"
                            f"{'已启用' if self._enabled else '已停用'}。\n"
                            f"搜索影视时将通过 "
                            f"{self._config.get('jackett_url')} 的 torznab 接口"
                            f"（索引器：{self._config.get('indexer') or 'all'}）"
                            f"返回全部聚合结果，站点名显示为"
                            f"「{self._config.get('site_name') or 'Jackett'}」。"
                        ],
                    }
                ],
            }
        ]

    # ------------------------------------------------------------------
    # 模块接入：注册为插件资源搜索源
    # ------------------------------------------------------------------
    def get_module(self) -> Dict[str, Any]:
        if not self._enabled:
            return {}
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    # ------------------------------------------------------------------
    # 搜索入口（框架在每次搜索时通过 search_plugin_torrents 调用）
    # ------------------------------------------------------------------
    def search_torrents(
        self,
        site: dict = None,
        keyword: str = None,
        mtype=None,
        page: int = 0,
    ) -> List[Any]:
        if not self._enabled or not keyword:
            return []
        try:
            return self._do_search(keyword=keyword, mtype=mtype)
        except Exception as exc:  # 单个源失败不影响其它站点
            logger.error("Jackett 搜索失败：%s", exc)
            return []

    async def async_search_torrents(self, **kwargs: Any) -> List[Any]:
        return await asyncio.to_thread(self.search_torrents, **kwargs)

    # ------------------------------------------------------------------
    # 核心实现
    # ------------------------------------------------------------------
    def _do_search(self, keyword: str, mtype) -> List[Any]:
        base = str(self._config.get("jackett_url") or "").rstrip("/")
        api_key = self._config.get("api_key")
        indexer = self._config.get("indexer") or "all"
        timeout = int(self._config.get("timeout") or 30)
        site_name = self._config.get("site_name") or "Jackett"
        if not base or not api_key:
            logger.warning("Jackett 地址或 API Key 未配置，跳过搜索")
            return []

        params: Dict[str, Any] = {"apikey": api_key, "t": "search", "q": keyword}
        if self._config.get("filter_by_type") and mtype is not None:
            cat = self._mtype_to_cat(mtype)
            if cat:
                params["cat"] = cat

        url = f"{base}/api/v2.0/indexers/{indexer}/results/torznab/api"
        self._last_keyword = keyword
        # 显式关闭代理：Jackett 在局域网，不能走 HTTP_PROXY(192.168.2.251:7890)
        resp = requests.get(
            url,
            params=params,
            timeout=timeout,
            verify=False,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        return self._parse(resp.text, site_name=site_name, host=base)

    @staticmethod
    def _mtype_to_cat(mtype) -> Optional[str]:
        try:
            v = mtype.value if hasattr(mtype, "value") else str(mtype)
        except Exception:
            v = str(mtype)
        if v in ("MOVIE", "电影"):
            return "2000"
        if v in ("TV", "电视剧"):
            return "5000"
        if v in ("MUSIC", "音乐"):
            return "3000"
        return None

    def _parse(self, xml_text: str, site_name: str, host: str) -> List[Any]:
        torrents: List[Any] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("Jackett 返回 XML 解析失败：%s", exc)
            return []

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            encl = item.find("enclosure")
            enclosure = (encl.get("url") if encl is not None else None) or item.findtext(
                "link"
            )
            if not enclosure:
                continue
            enclosure = self._fix_host(enclosure, host)

            size_text = item.findtext("size")
            try:
                size = float(size_text) if size_text else 0.0
            except (TypeError, ValueError):
                size = 0.0

            seeders = self._attr_int(item, "seeders")
            peers = self._attr_int(item, "peers")
            grabs = self._int(item.findtext("grabs"))
            pubdate = item.findtext("pubDate")
            description = item.findtext("description") or ""
            dv = self._attr_float(item, "downloadvolumefactor") or 1.0
            uv = self._attr_float(item, "uploadvolumefactor") or 1.0
            category = self._map_category(self._attr(item, "category"))
            page_url = self._fix_host(
                item.findtext("comments") or item.findtext("guid") or "", host
            )

            if TorrentInfo is None:
                continue

            torrents.append(
                TorrentInfo(
                    site=site_name,
                    site_name=site_name,
                    site_proxy=bool(self._config.get("proxy")),
                    title=title,
                    enclosure=enclosure,
                    page_url=page_url,
                    size=size,
                    seeders=seeders,
                    peers=peers,
                    grabs=grabs,
                    pubdate=pubdate,
                    description=description or None,
                    downloadvolumefactor=dv,
                    uploadvolumefactor=uv,
                    category=category,
                    labels=[site_name],
                )
            )
        logger.info("Jackett 返回 %d 条结果（关键词：%s）", len(torrents), self._last_keyword)
        return torrents

    # ------------------------------------------------------------------
    # torznab 属性解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _attr(item: ET.Element, name: str) -> Optional[str]:
        el = item.find(f"torznab:attr[@name='{name}']", _TORZNAB_NS)
        return el.get("value") if el is not None else None

    @staticmethod
    def _attr_int(item: ET.Element, name: str) -> int:
        return jackettindexer._int(jackettindexer._attr(item, name))

    @staticmethod
    def _attr_float(item: ET.Element, name: str) -> Optional[float]:
        v = jackettindexer._attr(item, name)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(v) -> int:
        try:
            return int(float(v)) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _map_category(cat: Optional[str]) -> Optional[str]:
        if not cat:
            return None
        c = str(cat)
        if c.startswith("2"):
            return "电影"
        if c.startswith("5"):
            return "电视剧"
        if c.startswith("3"):
            return "音乐"
        return None

    @staticmethod
    def _fix_host(url: str, host: str) -> str:
        """Jackett 可能返回 127.0.0.1/localhost 的下载链接，统一改写为配置的局域网地址。"""
        if not url:
            return url
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
            new_netloc = urlparse(host).netloc
            replaced = parsed._replace(netloc=new_netloc)
            return replaced.geturl()
        return url

    # 仅用于日志展示的临时关键词（search_torrents 内部调用 _do_search 时设置）
    _last_keyword = ""

# -*- coding: utf-8 -*-
"""
Jackett 索引器插件（MoviePilot v3）

把 Jackett 作为一个「站点」接入 MoviePilot：
- 插件启用时自动调用 SitesHelper.add_indexer() 注册 Jackett 索引器，
  并把站点写入站点表 —— 于是「站点管理」里会出现一个 Jackett 站点；
- 通过 get_module() 把 search_torrents 注册为插件资源搜索源，
  MoviePilot 搜索影视时会调用 search_plugin_torrents 合并插件结果，
  本插件查询 Jackett 的 torznab API（默认聚合全部索引器 indexer=all），
  把种子以 TorrentInfo 回灌搜索结果，且 site 指向自动注册的 Jackett 站点。

仓库结构（MoviePilot 插件市场格式）：
  package.v3.json                      <- 插件索引（市场清单），键/id 与目录名、类名保持一致
  plugins.v3/jackettindexer/__init__.py <- 插件源码

注意：MoviePilot 运行时以「类名」作为 running_plugins 的 key，而市场/URL 以「目录名/package id」
作为 plugin_id；二者必须完全一致，否则 /api/v1/plugin/form 会返回 404（配置加载失败）。
本插件目录名、类名、package id 统一为 jackettindexer。

已知实现约束（v3）：
- 站点搜索走 async_search_site_torrents -> async_execute_system_modules，**不调用插件模块**，
  因此 v3 无法像 v2 那样由插件「胁持」某个站点的搜索实现。
- 站点搜索由内置 SiteSpider（HTML 抓取）完成，MoviePilot v3 **没有通用 torznab 解析器**，
  所以自动注册的 Jackett 站点负责「在站点管理里可见、可管理」，真实检索结果仍由本插件提供。
"""

from __future__ import annotations

import asyncio
import logging
import re
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
# 自动注册站点使用的域名（站点管理里以此标识 Jackett）
_DEFAULT_SITE_DOMAIN = "jackett.indexer"


class jackettindexer(_PluginBase):
    # ---- 插件元信息（MoviePilot 后台展示用）----
    plugin_name = "Jackett 索引器"
    plugin_desc = "将 Jackett 作为站点接入 MoviePilot，返回全部索引器聚合结果。"
    plugin_version = "1.3.0"
    plugin_author = "Qiruizheng"
    author_url = ""

    # ---- 默认配置 ----
    _default_config = {
        "enabled": True,
        "jackett_url": "http://192.168.2.220:9117",
        "api_key": "irzy7mdb318o91wrwai1q9p91cdvvivr",
        "site_name": "Jackett",
        "site_domain": _DEFAULT_SITE_DOMAIN,
        "register_site": True,
        "indexer": "all",
        "timeout": 90,
        "filter_by_type": False,
        "proxy": False,
        "use_tmdb": True,
        "strict_match": True,
    }

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._config: Dict[str, Any] = dict(self._default_config)
        self._site_id: Optional[int] = None
        self._title_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = dict(self._default_config)
        if config:
            self._config.update(config)
        self._enabled = bool(self._config.get("enabled"))
        if self._enabled and self._config.get("register_site"):
            self._site_id = self._ensure_site()
        logger.info(
            "Jackett 索引器插件初始化完成，启用状态=%s，站点ID=%s",
            self._enabled,
            self._site_id,
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # 站点注册：让「站点管理」里出现 Jackett 站点
    # ------------------------------------------------------------------
    def _ensure_site(self) -> Optional[int]:
        """注册 Jackett 索引器并写入站点表，返回站点 ID（失败返回 None）。"""
        base = str(self._config.get("jackett_url") or "").rstrip("/")
        site_name = self._config.get("site_name") or "Jackett"
        api_key = self._config.get("api_key") or ""
        domain = str(self._config.get("site_domain") or _DEFAULT_SITE_DOMAIN).strip()
        url = base + "/"
        if not base:
            return None

        # 1) 注册索引器：使站点域名可被 MoviePilot 识别（add_site 校验依赖它）
        try:
            from app.application.site.sites import SitesHelper

            helper = SitesHelper()
            try:
                exists = helper.get_indexer(domain)
            except Exception:
                exists = None
            if not exists:
                helper.add_indexer(
                    domain,
                    {
                        "id": "jackett",
                        "name": site_name,
                        "url": url,
                        "public": True,
                    },
                )
                logger.info("已注册 Jackett 索引器：%s", domain)
        except Exception as exc:  # 注册失败不影响插件搜索能力
            logger.warning("注册 Jackett 索引器失败（不影响搜索）：%s", exc)

        # 2) 写入站点表：站点管理里即可见
        try:
            from app.db.oper.site import SiteOper

            oper = SiteOper()
            site = oper.get_by_domain(domain)
            if site is not None:
                return getattr(site, "id", None)
            ok, msg = oper.add(
                name=site_name,
                domain=domain,
                url=url,
                apikey=api_key,
                public=1,
                is_active=1,
                proxy=1 if self._config.get("proxy") else 0,
                render=0,
                note="由 jackettindexer 插件自动注册（结果由插件 torznab 检索提供）",
            )
            logger.info("写入 Jackett 站点：%s（success=%s）", msg, ok)
            site = oper.get_by_domain(domain)
            return getattr(site, "id", None) if site is not None else None
        except Exception as exc:
            logger.warning("写入 Jackett 站点失败（不影响搜索）：%s", exc)
            return None

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
                                                "label": "站点域名",
                                                "model": "site_domain",
                                                "placeholder": "jackett.indexer",
                                                "hint": "自动注册到站点管理时使用的唯一域名",
                                            },
                                            "id": "site_domain",
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
                                                "label": "超时(秒)",
                                                "model": "timeout",
                                                "type": "number",
                                                "hint": "indexer=all 聚合较慢，建议不小于 90",
                                            },
                                            "id": "timeout",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "label": "自动注册为站点",
                                                "model": "register_site",
                                                "hint": "启用后在「站点管理」中出现 Jackett 站点",
                                            },
                                            "id": "register_site",
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
                                            "component": "VSwitch",
                                            "props": {
                                                "label": "中文名转英文原名检索(TMDB)",
                                                "model": "use_tmdb",
                                                "hint": "Jackett 多为英文索引器，中文关键词会检索不到",
                                            },
                                            "id": "use_tmdb",
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "label": "过滤无关结果",
                                                "model": "strict_match",
                                                "hint": "剔除标题不含检索词的结果",
                                            },
                                            "id": "strict_match",
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
        domain = self._config.get("site_domain") or _DEFAULT_SITE_DOMAIN
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
                            f"检索地址：{self._config.get('jackett_url')} 的 torznab 接口"
                            f"（索引器：{self._config.get('indexer') or 'all'}）。\n"
                            f"站点管理：域名 {domain}"
                            f"，站点ID {self._site_id if self._site_id else '未注册'}。\n"
                            f"站点由本插件自动注册；检索结果由插件 torznab 聚合后回灌，"
                            f"并归属到该站点。"
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
        timeout = int(self._config.get("timeout") or 90)
        site_name = self._config.get("site_name") or "Jackett"
        if not base or not api_key:
            logger.warning("Jackett 地址或 API Key 未配置，跳过搜索")
            return []

        # Jackett 聚合的多为英文索引器，中文关键词匹配不到会退回返回各站最新种子，
        # 因此先经 TMDB 把中文名解析成英文原名，再交给 Jackett 检索。
        query = self._resolve_query(keyword, mtype)
        logger.info("Jackett 检索词：%s（原始关键词：%s）", query, keyword)

        params: Dict[str, Any] = {"apikey": api_key, "t": "search", "q": query}
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
        return self._parse(
            resp.text,
            site_name=site_name,
            host=base,
            match_query=query if self._config.get("strict_match") else None,
        )

    # ------------------------------------------------------------------
    # 关键词解析：中文 -> TMDB 英文原名
    # ------------------------------------------------------------------
    def _resolve_query(self, keyword: str, mtype=None) -> str:
        """把中文关键词解析为英文原名；已是英文或解析失败时原样返回。"""
        kw = (keyword or "").strip()
        if not kw or self._has_ascii_letters(kw):
            return kw
        if not self._config.get("use_tmdb"):
            return kw
        if kw in self._title_cache:
            return self._title_cache[kw]
        title = self._tmdb_en_title(kw, mtype)
        self._title_cache[kw] = title or kw
        return self._title_cache[kw]

    @staticmethod
    def _has_ascii_letters(text: str) -> bool:
        return any("a" <= c.lower() <= "z" for c in text if c.isalpha() and ord(c) < 128)

    def _tmdb_en_title(self, keyword: str, mtype=None) -> Optional[str]:
        """经 TMDB 把中文名解析为英文标题（先中文搜出 ID，再取 en-US 详情）。"""
        try:
            from app.core.config import settings

            api_key = getattr(settings, "TMDB_API_KEY", None)
            domain = getattr(settings, "TMDB_API_DOMAIN", None) or "api.tmdb.org"
            proxies = getattr(settings, "PROXY", None)
            if not api_key:
                return None
        except Exception as exc:
            logger.warning("读取 TMDB 配置失败：%s", exc)
            return None

        # 按媒体类型决定优先检索电影还是剧集
        order = ("movie", "tv")
        try:
            v = mtype.value if hasattr(mtype, "value") else str(mtype)
        except Exception:
            v = ""
        if v in ("TV", "电视剧"):
            order = ("tv", "movie")

        for media_type in order:
            try:
                r = requests.get(
                    f"https://{domain}/3/search/{media_type}",
                    params={"api_key": api_key, "query": keyword, "language": "zh-CN"},
                    proxies=proxies,
                    timeout=20,
                )
                results = (r.json() or {}).get("results") or []
                if not results:
                    continue
                tmdb_id = results[0].get("id")
                if not tmdb_id:
                    continue
                d = requests.get(
                    f"https://{domain}/3/{media_type}/{tmdb_id}",
                    params={"api_key": api_key, "language": "en-US"},
                    proxies=proxies,
                    timeout=20,
                ).json() or {}
                title = d.get("title") or d.get("name") or d.get("original_title") or d.get("original_name")
                if title:
                    logger.info("TMDB 解析：%s -> %s（%s）", keyword, title, media_type)
                    return str(title)
            except Exception as exc:
                logger.warning("TMDB 解析失败（%s）：%s", media_type, exc)
        return None

    # ------------------------------------------------------------------
    # 相关性过滤：剔除 Jackett 无匹配时回吐的无关种子
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(text: str) -> str:
        return " " + re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (text or "").lower()).strip() + " "

    @classmethod
    def _tokens(cls, query: str) -> List[str]:
        """英文按词切分（去停用词），中文按整串保留。"""
        raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", query or "").strip()
        if not raw:
            return []
        if cls._has_ascii_letters(raw):
            return [t for t in raw.lower().split() if t not in ("the", "a", "an", "of", "and")]
        return [raw.lower()]

    def _relevant(self, title: str, query: str) -> bool:
        """标题需包含检索词的全部有效词元，否则视为无关。"""
        tokens = self._tokens(query)
        if not tokens:
            return True
        norm = self._normalize(title)
        for t in tokens:
            if len(t) == 1:
                continue
            if t.isdigit():
                continue
            if (" " + t + " ") not in norm and t not in norm:
                return False
        return True

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

    def _parse(
        self,
        xml_text: str,
        site_name: str,
        host: str,
        match_query: Optional[str] = None,
    ) -> List[Any]:
        torrents: List[Any] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("Jackett 返回 XML 解析失败：%s", exc)
            return []

        # v3 的 TorrentInfo.site 必须是 int（站点ID），不能是字符串
        site_id = int(self._site_id or 0)
        total = 0

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            total += 1
            # 剔除与检索词无关的条目（Jackett 无匹配时索引器会回吐最新种子）
            if match_query and not self._relevant(title, match_query):
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
                    site=site_id,
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
        logger.info(
            "Jackett 返回 %d 条结果（原始 %d 条，关键词：%s，检索词：%s）",
            len(torrents),
            total,
            self._last_keyword,
            match_query or self._last_keyword,
        )
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

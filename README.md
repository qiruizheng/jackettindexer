# Jackett 索引器（MoviePilot v3 插件）

把 [Jackett](https://github.com/Jackett/Jackett) 作为一个「站点」接入 MoviePilot v3：
插件启用后自动在**站点管理**里注册一个 Jackett 站点，搜索影视时返回 Jackett 聚合的全部索引器结果，
且结果归属到该站点。

## 功能

- **自动注册站点**：启用插件即调用 `SitesHelper.add_indexer()` 注册索引器、写入站点表，
  「站点管理」里出现 Jackett 站点（默认域名 `jackett.indexer`），无需手动添加；
- 注册 `search_torrents` 插件搜索源，MoviePilot 每次影视搜索自动调用；
- 调用 Jackett 的 torznab API（默认 `indexer=all` 聚合全部索引器）；
- 结果以 `TorrentInfo` 回灌，`site` 指向自动注册的 Jackett 站点 ID，`site_name` 为「Jackett」；
- 自动把 Jackett 返回的 `127.0.0.1/localhost` 下载链接改写为局域网地址，确保可下载。

## 仓库结构（MoviePilot 插件市场格式）

```
package.v3.json                        # 插件市场索引（清单）
plugins.v3/jackettindexer/__init__.py  # 插件源码
```

> 目录名、类名、`package.v3.json` 的键/id 必须三者一致（此处均为 `jackettindexer`），
> 否则 `/api/v1/plugin/form` 会 404（后台显示「配置加载失败」）。

## 安装（MoviePilot 后台）

1. 系统设置 → 插件 → 插件市场，追加本仓库地址：
   `https://github.com/<你的用户名>/<本仓库名>`
2. 刷新市场，找到「Jackett 索引器」并安装、启用；
3. 启用后到**站点管理**即可看到自动注册的 Jackett 站点；
4. 搜索影视，结果中 `site_name = Jackett` 的条目即全部 indexer 的聚合结果。

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用搜索 | `true` |
| `jackett_url` | Jackett 地址 | `http://192.168.2.220:9117` |
| `api_key` | Jackett API Key | `your key` |
| `site_name` | 站点名称 | `Jackett` |
| `site_domain` | 注册站点的唯一域名 | `jackett.indexer` |
| `register_site` | 是否自动注册为站点 | `true` |
| `indexer` | 索引器 ID，`all` 表示全部 | `all` |
| `timeout` | 请求超时（秒），`all` 聚合较慢 | `90` |
| `filter_by_type` | 按媒体类型过滤分类 | `false` |
| `proxy` | 是否走代理下载 | `false` |

> Jackett 在局域网时务必将 `proxy` 保持关闭，否则会因容器 `HTTP_PROXY` 导致请求失败。

## v3 实现说明（重要）

- v3 的站点搜索走 `async_search_site_torrents → async_execute_system_modules`，
  **不会调用插件模块**，因此无法像 v2（JackettExtend）那样由插件接管站点搜索实现。
- v3 内置站点解析器是 `SiteSpider`（HTML 抓取），**没有通用 torznab 解析器**，
  内置支持的 194 个站点全是具体 tracker 域名，直接把 Jackett 的 torznab URL 当站点添加会被拒绝
  （`该站点不支持，请检查站点域名是否正确`）。
- 因此本插件的站点负责「在站点管理里可见/可管理」，真实检索结果由插件的 torznab 聚合提供，
  并回填站点 ID 归属该站点。

## 兼容性

- 仅适配 MoviePilot **v3**（声明 `v3: true`）。
- 依赖容器内 `requests`（已随 MoviePilot 镜像提供）。

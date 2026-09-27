# Jackett 索引器（MoviePilot v3 插件）

把 [Jackett](https://github.com/Jackett/Jackett) 作为一个「站点」接入 MoviePilot v3 的统一搜索，
让 MoviePilot 在搜索影视时返回 Jackett 聚合的全部索引器结果。

## 功能

- 通过插件机制注册 `search_torrents`，在 MoviePilot 每次影视搜索时自动调用；
- 调用 Jackett 的 torznab API（默认 `indexer=all` 聚合全部索引器）；
- 将返回的种子以 MoviePilot 标准 `TorrentInfo` 回灌进搜索结果，站点名显示为「Jackett」；
- 支持配置 Jackett 地址、API Key、索引器 ID、超时、是否走代理、是否按媒体类型过滤分类；
- 自动把 Jackett 返回的 `127.0.0.1/localhost` 下载链接改写为局域网地址，确保可下载。

## 仓库结构（MoviePilot 插件市场格式）

```
package.v3.json                        # 插件市场索引（清单）
plugins.v3/jackettindexer/__init__.py  # 插件源码
```

## 安装（MoviePilot 后台）

1. 系统设置 → 插件 → 插件市场，追加本仓库地址：
   `https://github.com/<你的用户名>/<本仓库名>`
2. 刷新市场，找到「Jackett 索引器」并点击安装；
3. 启用插件，按需修改配置（默认已填 `http://192.168.2.220:9117` 与你的 API Key）；
4. 在 MoviePilot 搜索影视，结果中即可看到 `site = Jackett` 的条目。

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用搜索 | `true` |
| `jackett_url` | Jackett 地址 | `http://192.168.2.220:9117` |
| `api_key` | Jackett API Key | 你的 key |
| `site_name` | 搜索结果中的站点名 | `Jackett` |
| `indexer` | 索引器 ID，`all` 表示全部 | `all` |
| `timeout` | 请求超时（秒） | `30` |
| `filter_by_type` | 按媒体类型过滤分类 | `false` |
| `proxy` | 是否走代理下载 | `false` |

> Jackett 在局域网时务必将 `proxy` 保持关闭，否则会因容器 `HTTP_PROXY` 导致请求失败。

## 兼容性

- 仅适配 MoviePilot **v3**（声明 `v3: true`）。
- 依赖容器内 `requests`（已随 MoviePilot 镜像提供）。

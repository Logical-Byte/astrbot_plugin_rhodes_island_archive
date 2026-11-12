# 罗德岛数据文献馆

AstrBot 的明日方舟数据查询插件。通过明日方舟知识库 MCP 查询干员、材料、关卡、敌人、公招和集成战略等资料，并使用服务返回的图片卡片展示结果。

## 配置

在 AstrBot 插件配置中设置：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| MCP 服务地址 | 支持 Streamable HTTP 的明日方舟知识库地址 | `http://bd4wxr.top:40317/mcp` |
| 请求超时 | MCP 连接和查询超时，单位为秒 | `30` |

插件运行环境需要能够访问配置的 MCP 服务。公招截图识别使用本地 OCR，依赖在 `requirements.txt` 中声明。

## 查询指令

下列示例省略唤醒前缀；前缀由 AstrBot 和对应平台处理。名称、别名或 ID 可用于查询；唯一的精确结果直接展示，其余匹配结果展示选择卡片。

| 指令示例 | 查询内容 | MCP 工具 |
| --- | --- | --- |
| `迷迭香` / `干员 迷迭香` | 干员基础资料 | `get_operator_basic_data` |
| `干员材料 迷迭香` / `迷迭香材料` / `技能 迷迭香` / `专精 迷迭香` / `迷迭香专精` | 干员养成材料（含精英化、技能升级、专精、模组） | `get_operator_material` |
| `模组 迷迭香` / `迷迭香模组` | 干员模组 | `get_operator_modules` |
| `召唤物 迷迭香` | 干员召唤物 | `get_token_detail` |
| `皮肤 迷迭香` / `迷迭香皮肤` | 干员皮肤 | `get_operator_skins` |
| `材料 固源岩` | 材料详情 | `get_material` |
| `关卡 1-7` | 关卡详情 | `get_stage_data` |
| `敌人 <名称>` | 敌人详情 | `get_enemy_data` |
| `公招 高资 输出` | 公开招募组合 | `recruit_with_tag` |
| `盟约 <名称>` | 卫戍协议盟约 | `get_autochess_bond_detail` |
| `策略 <名称>` | 卫戍协议策略 | `get_autochess_strategy_detail` |
| `肉鸽 <名称>` / `集成战略 <名称>` / `藏品 <名称>` | 集成战略物品与藏品（含通宝、骰子、构想等） | `get_integrated_strategy_item_detail` |
| `搜索 <名称>` | 搜索并展示候选 | `search`、`build_search_card` |
| `搜索卡 <ID> [ID…]` | 按 ID 展示选择卡 | `build_search_card` |
| `术语 <名称>` | 查询术语解释 | `get_glossary` |

未识别的多词消息会回复「没有找到」，单个词不匹配时保持安静；`终末地` 相关查询已停用。

搜索卡支持选择按钮，也可以发送 `选择 1` 这样的编号。服务端的搜索结果可能包含模糊匹配；插件不会自动将单个模糊结果当作精确结果。

干员卡、召唤物卡与按钮行合并在同一条消息里（图片用 Markdown 内联图），不再拆成多条。干员卡提供模组、皮肤、材料等按钮。按钮和最近一次干员卡的快捷查询有效期为 **5 分钟**，过期后可重新查询。皮肤选择卡支持按编号查看各套皮肤。

## 公开招募截图

1. 发送公招截图，再发送 `公招`；或先发送 `公招`，然后在 **30 秒内**发送截图。
2. 插件读取同一会话中发送者最近的一张图片，通过本地 OCR 识别标签。
3. 完整识别到五个标签后，将标签交给 MCP 计算；截图不完整时可在提示后重发。

也可直接发送 `公招 资深 输出`、`高资 治疗` 等文字标签。支持的标签来自 MCP 接口；服务端未识别的标签会在结果中提示。

## 关键词直达

内置一张可改写的关键词表，命中后直接回链接；标为图片的规则会把图片下载后作为图片消息发送，每次重新获取，不做缓存。

| 关键词 | 目标 |
| --- | --- |
| 一图流 | https://ark.yituliu.cn/ |
| 商店性价比 / 一图流商店性价比 | https://ark.yituliu.cn/material/store |
| 礼包性价比 / 一图流礼包性价比 | https://ark.yituliu.cn/material/pack |
| 价值 / 物品价值 | https://ark.yituliu.cn/material/value |
| 攒抽 / 攒抽计算器 | https://ark.yituliu.cn/tools/gachaCalc |
| 特米米 | https://tomimi.cyou/zh |
| 企鹅物流 | https://penguin-stats.io/ |
| 终末地一图流 | https://ef.yituliu.cn/ |
| 蓝图站 / 终末地蓝图站 | https://talospioneers.com/zh-CN |
| 终末地wiki | https://warfarin.wiki/cn |
| 热能池 / 热能池规划 | https://dige.aunly.cn/?lang=zh |
| 材料 / 活动 / 复刻 | 图片，默认 `http://bd4wxr.top:5040/api/files?name=材料` |
| 进店 | 图片，默认 `http://bd4wxr.top:5040/api/files?name=进店` |

表存在配置项 `links`（JSON 数组，字段 `label` / `pattern` / `url` / `image`），`pattern` 为整体匹配的正则。部署在内网时可把地址替换为内网地址。单发「模组」「皮肤」「召唤物」这类需要干员名的词不会匹配；「材料」本身就是图片直达，与干员卡无关。

## 说明

- `mcp_url` 必填，填 MCP 服务的完整地址（以 `/mcp` 结尾）。
- 读取图片尺寸失败时照常发图，只是省略宽高提示。


- 图片和查询结果以 MCP 服务返回的内容为准。QQ 官方平台的图片卡片与按钮使用同一条 Markdown 消息。
- 终末地、独立技能、语音、阵营及档案查询当前停用。
- 搜索能力取决于 MCP 服务提供的数据和匹配结果。

## 说明

- `mcp_url` 必填，填 MCP 服务的完整地址（以 `/mcp` 结尾）。
- 读取图片尺寸失败时照常发图，只是省略宽高提示。


- 数据服务超时或不可达时统一回复「数据服务暂时没有响应，请稍后再试。」，不展示内部错误与堆栈。

## 鸣谢

- [Amiya-Bot-mcp-server](https://github.com/xingzhiyou/Amiya-Bot-mcp-server)：明日方舟数据和卡片服务。
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：插件框架与消息平台适配。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：公招截图文字识别。

## 许可证

[MIT](LICENSE)

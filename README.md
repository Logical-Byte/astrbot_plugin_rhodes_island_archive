# 罗德岛数据文献馆

明日方舟与终末地的数据与资源调用插件。**当前只实现资源调用层和一个静默匹配监听器**：没有注册任何指令名，也没有命令表。

## 目录结构

```
astrbot_plugin_rhodes_island_archive/
├── metadata.yaml        # 插件元数据
├── main.py              # 插件入口：构造 provider，命中后渲染卡片 PNG，不注册指令
├── models.py            # GameDataEntry / ResourceRef
├── _conf_schema.json    # 超时、缓存、下载上限、User-Agent、渲染超时
├── requirements.txt     # playwright（本地渲染；浏览器需另行安装）
├── README.md
├── assets/
│   ├── operator_bg.png        # 模板底纹（随模板内联）
│   ├── rank/1.png … 6.png     # 潜能徽章（模板自带，含黑底）
│   └── HarmonyOS_Sans_SC.ttf  # 模板指定字体
└── providers/
    ├── __init__.py
    ├── base.py          # GameDataProvider 抽象接口
    ├── net.py           # 共享 httpx 客户端 + 内存/磁盘两级缓存
    ├── card.py          # 模板 HTML + CSS（移植自 AmiyaBot operatorArchives）
    ├── render.py        # 本地无头 Chromium 渲染（Playwright）
    ├── arknights.py     # 明日方舟：torappu.prts.wiki
    └── endfield.py      # 终末地：data.akedata.wiki
```

## 接口

每个 provider 都实现同一组异步方法：

| 方法 | 说明 |
| --- | --- |
| `search(keyword, limit=5)` | 按名称或 ID 检索角色 |
| `get_detail(entry_id)` | 按精确 ID 取角色，原始字段放在 `attributes` |
| `match(text)` | 单个词的静默匹配：完全匹配优先，部分匹配仅在唯一命中时采用 |
| `card(entry)` | 组装卡片数据（数值、档案、潜能、天赋、基建、技能、立绘） |
| `token_card(entry)` | 组装装置页数据（无装置时返回 `None`） |
| `list_dir(path)` | 列出数据源的一个目录 |
| `locate(kind, key)` | 按资源类型定位文件 |
| `fetch(ref)` | 下载 `ResourceRef` 指向的文件 |

`ResourceRef` 含 `game / kind / path / url / size / is_dir`。`fetch` 会重新校验 `game`、路径与 URL 主机，只下载本 provider 生成的引用。

## 数据源

- 明日方舟：`https://torappu.prts.wiki`，`gamedata/` 为解包后的 JSON 表，`raw/` 下的资源通过 `/assets/` 下载。
- 终末地：`https://data.akedata.wiki`，`manifest.json` 给出版本与 `tableCfgPath`，`asset-sync-index.json` 索引全部静态图片与 JSON。

`locate` 支持的 `kind`：

- 明日方舟：`table`、`asset`、`directory`、`portrait`、`avatar`、`arts`
- 终末地：`images`、`json`、`table`、`portrait`、`avatar`、`roundicon`

## 静默匹配

唤醒词后面只跟一个词时触发，规则如下：

1. 先做完全匹配：中文名、英文名（`appellation` / `engName`）、角色 ID，忽略大小写与首尾空格。
2. 完全匹配没有命中，才做部分匹配；**只有唯一命中**时采用。
3. 仍然没有命中，或部分匹配命中多个，**不输出任何内容**，也不提示「未找到」。
4. 明日方舟优先匹配，未命中再看终末地。

命中后回复**一张卡片图片**。模板移植自 [AmiyaBot operatorArchives](https://github.com/AmiyaBot/Amiya-Bot-plugins/tree/e7cf5a426cfd2458f3b3e376734d79779fe77da2/src/arknights/operatorArchives) 的 `operatorInfo.html` / `operatorInfo.css`，样式规则逐条照搬，底纹、职业图标、技能图标、潜能图标与**立绘**都内联后写进文档，渲染时不需要回源。

卡片里的图标全部内联：职业图标与技能图标取自 `raw/profession_icon`、`raw/skill_icon`，基建技能图标取自 `raw/build_skill_icon/<skillIcon>.png`（`building_data.buffs[].skillIcon`，每个技能一枚黄色圆章；该字段缺失时才退回 `building_buff_icon/<room>.png`，并把白色字形重绘成深色，否则在白底上看不见），潜能提升用模板自带的 `assets/rank/N.png` 黑底徽章。描述里的强调标记（`<@cc.kw>` 等）会转成 `<span class="mark">`，按模板的 `#00b3ff` 高亮。

渲染优先用**本地无头 Chromium**（Playwright），并把模板指定的 `HarmonyOS_Sans_SC.ttf` 放在文档同目录用相对路径引用，字体与排版和原模板一致；本地不可用时自动改用 AstrBot 的文转图服务（这条路径没有 HarmonyOS 字体，会退回系统字体栈）。立绘内联前会缩到 700 px 并转成 JPEG：即使是本地渲染，兆级 base64 也会让页面变慢，而卡片里立绘的显示宽度约 690 px，700 px 仍然清晰。

本地渲染需要一次安装浏览器：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

插件会在自身数据目录下寻找 `PLAYWRIGHT_BROWSERS_PATH` 指向的浏览器；用
`PLAYWRIGHT_BROWSERS_PATH=<插件数据目录>/browsers python -m playwright install chromium`
安装到插件目录时无需再设环境变量。

**渲染失败就不回复**，不回退成文本。渲染结果按角色缓存为 `cache/images/card_v3_<game>_<id>.png`，同一角色第二次命中直接发这张图，不再调用渲染器；改动模板后递增文件名前缀即可让旧图失效。

下面是同一份卡片渲染出的文本等价内容（便于核对字段来源）：

### 明日方舟

```
迷迭香
RE45 Rosmontis
6★ · 狙击 - 投掷手 · 远程位 · 输出
---
最大生命值 1944
攻击力 688+60
防御力 245+30
魔法抗性 15
攻击速度 100
攻击间隔 2.1
阻挡数 1
部署费用 25
再部署时间 70秒
---
势力 罗德岛
阵营 罗德岛-精英干员
队伍 未知
种族 菲林
画师 唯@W
生日 7月6日
真名 未知
CV 中文-普通话 张若瑜 / 日文 小仓唯 / 韩文 尹垠舒 / 英文 Hayden Daviau
---
潜能提升
1 部署费用-1
2 再部署时间-4秒
---
攻击范围
■■■··
■■■■·
■■■■■
■■■■·
■■■··
---
特性
攻击对小范围的地面敌人造成两次物理伤害（第二次为余震，伤害降低至攻击力的一半）
---
干员印象
罗德岛精英干员迷迭香，用手推开了你的办公室门。
「她没带装备，不用害怕她记得你昨天偷吃了她的点心的事，也许。」
---
信物
迷迭香的信物
一件精心制作但水平依然还很业余的植物标本。她真心希望你能帮她记住这些。
---
天赋
歼灭战装备 攻击无视目标175（+15）防御力
感知稳定 部署后随机选择一名在场的【术师】干员，自身和其的攻击力+8%
---
基建技能
精英0 超感  进驻制造站时，宿舍内每有1名干员则感知信息+1，同时每1点感知信息转化为1点思维链环
---
技能1：思维膨大
攻击回复 自动触发 技力 0/2
下次攻击额外造成一次相当于攻击力180%的法术伤害
攻击范围
■■■··
■■■■·
■■■■■
■■■■·
■■■··
```

- 表头三行：名称、`displayNumber + appellation`、`星级 · 职业 - 子职业 · 站位 · 标签`；子职业取自 `uniequip_table.subProfDict`，标签来自 `tagList`，异格干员额外加「异格干员」。
- 数值九行：满级取 `phases[-1].attributesKeyFrames[-1].data`，信赖加成取 `favorKeyFrames[-1].data`，显示为 `基础+加成`，无加成时只显示基础值；`再部署时间` 以秒为单位。
- 档案八行：势力 / 阵营 / 队伍取自 `handbook_team_table` 的 `powerName`；种族、生日、真名从 `handbook_info_table` 的「基础档案」文本里按 `【键】值` 解析；画师取 `skin_table.charSkins.<id>.displaySkin.drawerList[0]`；CV 取 `charword_table.voiceLangDict.<id>.dict` 的四种语音语言。缺值时打印 `未知`，与参考卡片一致。
- 潜能提升：`potentialRanks[*].description`，按序号逐行输出。
- 攻击范围：`range_table[rangeId].grids`，`■` 为可攻击格，按行拼接。
- 特性：`character_table[<id>]["description"]`，会剥掉 `<@ba.kw>` 一类富文本标签。
- 干员印象：`itemUsage` 一行，`itemDesc` 加书名号一行。
- 信物：`potentialItemId` 指向 `item_table.items.<id>` 的 `name` 与 `description`。
- 天赋：`talents[*].candidates[-1]`，输出名称与描述，`blackboard` 用于填充占位符。
- 基建技能：`building_data.chars.<id>.buffChar` 关联 `building_data.buffs`，输出 `精英阶段 技能名 描述`。
- 技能：每个技能取 `skill_table[<skillId>].levels[-1]`，输出技能名、回复类型、触发类型、`初始技力/消耗技力`、描述与攻击范围。
- 描述里的 `{atk:0%}` 一类占位符按该技能 `blackboard` 填充；百分比按四位有效数字渲染，避免浮点噪声。
- 技能没有自带 `rangeId` 时，攻击范围回落到干员自身范围。
- 1★ 小车一类的干员本身没有技能，卡片只有表头与数值。

### 装置页

装置不再挤在干员卡片里，而是单独出图，版式照搬参考插件的 `operatorToken.html`：1280×720、深色底图、标题是「{干员名} 的召唤物」。

```
迷迭香 的召唤物
┌ 头像 100px ┐ 迷迭香的战术装备  Rosmontis-gear  [近战位]
              能够阻挡两个敌人，降低被阻挡单位的防御力
              ┌ 最大生命值 5000 │ 攻击力 100 ┐
              │ 防御力 520      │ 魔法抗性 0 │
              │ 攻击间隔 1      │ 阻挡数 2   │
              └ 部署费用 0      │ 再部署时间 0 ┘
[攻击范围] ■
[技能] 战术装备召唤 [自动回复][自动触发][技力 0/2]
       出现时晕眩周围敌人3秒
```

| 区块 | 字段 | 来源 |
|---|---|---|
| 标题头像 | 干员头像 | `raw/char_avatar/<charId>.png` |
| 装置头像 | `token_*` 头像 | `raw/char_avatar/<tokenId>.png`，缺失时取同前缀的第一个 |
| 名称 | 中文名 / 英文名 | `character_table[<tokenId>].name` / `.appellation` |
| 站位 | 近战位 `#f44336` / 远程位 `#4caf50` / 不限部署位 `#9c27b0` | `character_table[<tokenId>].position` |
| 特性 | 一行黄字 | `character_table[<tokenId>].description` |
| 数值 | 最大生命值 / 攻击力 / 防御力 / 魔法抗性 / 攻击间隔 / 阻挡数 / 部署费用 / 再部署时间 | `phases[-1].attributesKeyFrames[-1].data` |
| 攻击范围 | 白块网格 | `range_table[<rangeId>].grids` |
| 天赋 | 名称 + 描述 | `talents[*].candidates[-1]`，`-` 之类的占位描述会被跳过 |
| 技能 | 名称 + 徽章 + 描述 | `skill_table[sktok_*].levels[-1]`；同一技能只出一次，没有描述的等级跳过 |

装置来源是干员记录里的 `displayTokenDict`（467 个干员中 54 个有装置，共 54 页、62 个技能）。没有装置的干员只回卡片，不额外发图。

### 公招

发送「公招」加标签，插件按标签组合算出能锁定稀有干员的方案，输出一页图。

```
公招 资深 输出
公招 高资 治疗
公招 控场 减速
```

- 标签：职业（先锋/近卫/重装/狙击/术师/医疗/辅助/特种）、位置（近战位/远程位）、特性（输出/生存/治疗/防护/削弱/群攻/减速/快速复活/召唤/支援/费用回复/控场/爆发/位移/元素/支援机械），以及 `新手` / `资深干员` / `高级资深干员`；`资深`、`高资`、`高级资深` 会被自动补全。
- 干员的标签集合 = `character_table[].tagList` ∪ 职业 ∪ 位置，稀有度标签按星级补：6★ 记 `高级资深干员`、5★ 记 `资深干员`、≤2★ 记 `新手`。
- 组合：输入标签的全部 1–3 元组合（`高级资深干员` 与 `资深干员` 不会同时出现在一组），按「标签数降序、最高星级降序」排列。
- 每组只列该组合能锁到的干员：组合里的标签必须全部被干员满足，且不低于该组的下限星级（有 `高级资深干员` 为 6★、有 `资深干员` 为 5★、否则 4★）；6★ 只在含 `高级资深干员` 的组合里出现。
- 卡面沿用装置页的深色底图：每组一个标签条 + 60px 头像，星级用底边颜色区分（6★ `#f57c00`、5★ `#ffc107`、4★ `#d1c4e9`）。
- 没有组合能锁到 4★ 以上时，回一句纯文本，不出图。
- 候选干员取自 `gacha_table.recruitDetail` 里的官方公招池（当前 163 人：1★8 / 2★5 / 3★16 / 4★42 / 5★56 / 6★36），不是「所有可获得干员」——限定干员不进公招，也绝不会出现在列表里。
- 判定规则与源插件一致：组合须覆盖干员命中的全部标签（`all_match` 的字符串包含判定）；只列稀有度 ≥ 4 的干员；6★ 还要求组合含「高级资深干员」；遇到首个 3★ 即停止遍历该组合（源插件的 `break`）。
- 页面骨架照搬 `template/operatorRecruit.html`：每组一列，左侧 5px 竖条，标签胶囊在上、干员在下；干员用 `char_portrait/<id>_1.png` 半身像，底边按星级着色；不设标题，也不截断。
- 无组合时回「博士，没有找到可以锁定稀有干员的组合」；查不到干员回「博士，无法查询到标签所拥有的稀有干员」。

**截图识别（本地 OCR）**：只发「公招」两个字带一张公招界面截图，插件会先下载图片，用本地 `rapidocr-onnxruntime`（ONNX Runtime + PP-OCR 模型，纯 CPU、无需联网、无需 API key）识别文字，再用「最长优先」匹配标签词表（`高级资深干员` 会先于 `资深干员` 命中）。识别不到标签会回一句提示。

OCR 是可选依赖：

```bash
pip install rapidocr-onnxruntime
```

没装时插件照常加载，只是走截图这条路会提示「本地 OCR 未安装」。首次调用会初始化引擎（约 2 秒），之后常驻进程内复用。

### 终末地

终末地走「档案」版式：左侧是数值与档案，右侧是立绘。

| 区块 | 字段 | 来源 |
|---|---|---|
| 数值 | 生命值 / 攻击力 | `CharacterTable[].attributes[-1]` 的 `attrType` 1、2 |
| 属性 | 力量 / 敏捷 / 智识 / 意志 | 同帧的 `attrType` 39–42 |
| 档案 | 阵营 / 种族 / 生日 | `profileRecord[].recordDesc` 里的「基础档案」`【键】值` |
| 档案 | 职业 / 主能力 / 副能力 | `CharProfessionTable`、`mainAttrType`、`subAttrType` |
| 档案 | 武器 | `defaultWeaponId` → `ItemTable` 中文名，缺失时退回 `WeaponBasicTable.engName` |
| 潜能提升 | 潜能 1–5 名称 | `CharacterPotentialTable[].potentialUnlockBundle[].name` |
| CV | 中 / 日 / 韩 / 英 | `CharacterTable[].cvName` 四个 id → `I18nTextTable_CN` |

不输出的项：防御力（`attrType` 3 在全部 35 个角色上都是 `0`，打印等于编造）；画师、专长、爱好（`CharacterTagTable` 只存 `tag_expert_*` 这类字符串 id，中文文本不在任何已发布表里，对应的 `public/CH/v2_character/*.json` 目前 404）。信赖、模组、技能在终末地侧尚未接入。

## 缓存

两级缓存，目录为 `data/plugin_data/astrbot_plugin_rhodes_island_archive/cache/`：

1. **内存**：命中后 `cache_ttl` 秒内直接返回，不发请求。
2. **磁盘**：每个 URL 存一份负载（`<sha256 前 40 位>.bin`）和一份元数据（`.json`，含 `ETag`、`Last-Modified`、写入时间）。
   - 未超过 `cache_ttl`：直接读盘，零请求。
   - 已过期：发一次 `HEAD`，比对 `ETag` / `Last-Modified`；没变就刷新写入时间，不变体。
   - 变了或 `HEAD` 失败：整份重新下载。

终末地镜像不支持 `If-None-Match`（带条件头仍返回 200 全量内容），所以用 `HEAD` 而不是 304 做校验。

### 立绘

立绘不经 URL 直发，而是先下载进 `cache/images/`，**原样保存 PNG**（不缩放、不转码），之后以本地文件发送：
`arknights_char_391_rosmon.png`（135,121 B）、`endfield_chr_0004_pelica.png`（16,788,036 B）。

如果不落盘、每次把 URL 丢给平台，平台每次都要重新下载：终末地立绘是 2048×2048 的 PNG，单张 16 MB，冷下载实测 14.7 s。落盘后同一张图再次回复是本地读文件，不再触网。

## 日志

排查时把日志级别调到 `DEBUG`（AstrBot 控制台默认 `INFO`），会多出每个上游请求一行：

```
[DEBUG] GET https://torappu.prts.wiki/gamedata/.../excel/character_table.json
[DEBUG] GET https://torappu.prts.wiki/gamedata/.../excel/character_table.json -> 200 in 0.42s (8164888 bytes)
[DEBUG] disk hit https://data.akedata.wiki/public/.../CharacterTable.json (10978089 bytes)
```

`INFO` 级别每个角色两行，足以看出一张卡片卡在哪一步：

```
card arknights/char_391_rosmon: collecting data
card arknights/char_391_rosmon: data 0.39s, html 461 KB, render 9.44s, png 792 KB
card arknights/char_391_rosmon: cache hit card_v2_arknights_char_391_rosmon.png (810743 bytes)
```

渲染失败会打一条 `WARN`，带名字、耗时、模板体积和异常类型；异常不再冒泡成平台层的「运行失败」：

```
card arknights/char_391_rosmon: render failed after 0.00s, html 472327 bytes: TimeoutError: render service timed out
card for arknights/char_391_rosmon failed: TimeoutError: render service timed out
```

未命中任何角色时只有一行 `DEBUG: no character matched '...'`，不会回复。

## 已知限制

- 终末地的 `TableCfg` 目录无法枚举，`locate("table", ...)` 按固定文件名拼接，实际是否存在需由 `fetch` 的 HTTP 状态确认。
- 终末地角色表的 `name.text` 为空，中文名来自同版本目录下的 `I18nTextTable_CN.json`（约 16.5 万条、13 MB），首次匹配时下载并缓存；`attrType` 仍然没有官方名称映射。
- 明日方舟镜像的目录接口要求整段路径做 URL 编码（`/` 编码为 `%2F`），且目录项不含文件哈希。
- 磁盘缓存没有容量上限，只按 URL 覆盖写入；需要清理时直接删 `cache/` 目录。
- 卡片需要九张表，解析结果在 provider 生命周期内保留（约 140 MB），首次出卡约 0.3 s，之后约 0.01 s；`charword_table`（50 MB）与 `skin_table`（11 MB）只取出 CV 与画师后即释放。
- 卡片是图片：优先用本地 Chromium 渲染（首次约 1.5–2.5 s，其中浏览器启动约 1 s），本地不可用时退回 AstrBot 的文转图服务（约 5–15 s）；两条路径都不通时不出卡。缓存后重复命中约 0.0 s。
- 卡片宽度固定 1400 px，高度随技能数量变化（无技能约 820 px，三个技能约 1200 px）。
- 立绘内联副本是 700 px JPEG（约 60 KB），与 `cache/images/` 里保留的原始 PNG 是两份文件：前者只用于卡片渲染，后者是原始资源。
- 静默匹配只认单个词；带空格或含 `/` 的消息不参与匹配。
- 上游异常时只写日志、不回复，避免网络故障时刷屏。

## 待办

- [ ] 指令层与 `views/` 可视化页面

## 版本号

每次改动都必须同时提升 `metadata.yaml` 的 `version`，并在提交信息里注明升到哪个版本：

| 变更类型 | 版本位 |
|---|---|
| 修 bug、调文案 | 末位 +1（1.2.3 → 1.2.4） |
| 新功能、新面板 | 中位 +1（1.2.3 → 1.3.0） |
| 破坏性改动、配置项重命名 | 首位 +1（1.2.3 → 2.0.0） |

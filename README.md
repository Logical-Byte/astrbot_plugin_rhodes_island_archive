# 罗德岛数据文献馆

明日方舟与终末地的数据查询插件：唤醒词后跟一个角色名，回复一张干员卡；卡后还可以查看装置页与模组、皮肤、档案、阵营、语音五个子页面，以及公开招募标签组合。

数据取自公开镜像，图片在本地用无头 Chromium 渲染，不依赖在线文转图服务（不可用时才回退）。

## 触发

没有固定指令名，靠三条正则：

| 触发 | 行为 |
| --- | --- |
| 唤醒词 + 单个角色名 | 回复干员卡；未命中**不回复** |
| `材料` + 名称 | 材料档案：简介、用途、掉落关卡、加工配方 |
| 单个材料名（如 `固源岩`） | 同上；角色名优先，角色查不到才当材料 |
| `技能` + 干员名 / 技能名 | 技能页；按干员名或技能名都能查到 |
| 单个技能名（如 `思维膨大`） | 同上；依次尝试角色、材料、技能 |
| `干员材料` + 干员名，或 `干员名材料` | 该干员的精英化 / 技能升级 / 技能专精 / 模组材料 |
| `干员技能` + 干员名，或 `干员名技能` | 该干员的三个技能 |
| `公招` / `公开招募` / `高资` / `高姿` + 标签 | 回复公开招募方案 |
| `模组` / `皮肤` / `档案` / `阵营` / `语音` | 为最近出过卡的干员出子页面；可加 `干员` 前缀 |

角色名匹配：先完全匹配（中文名、英文名、角色 ID，忽略大小写），未命中才做部分匹配，且**只在唯一命中时**采用；仍无命中或命中多个则静默。明日方舟优先，其次终末地。

## 目录结构

```
astrbot_plugin_rhodes_island_archive/
├── metadata.yaml
├── main.py                # 入口：三个监听、按钮分发、渲染与缓存
├── models.py              # GameDataEntry / ResourceRef
├── _conf_schema.json      # 超时、缓存、下载上限、UA、渲染超时
├── requirements.txt       # playwright（渲染）、rapidocr-onnxruntime（OCR，可选）
├── assets/
│   ├── operator_bg.png         # 干员卡底纹
│   ├── pc_bg.jpeg              # 装置页 / 子页面 / 公招底图
│   ├── rank/1.png … 6.png      # 潜能徽章（含黑底）
│   └── HarmonyOS_Sans_SC.ttf   # 模板指定字体
└── providers/
    ├── base.py            # GameDataProvider 抽象接口
    ├── net.py             # 共享 httpx 客户端 + 内存/磁盘缓存
    ├── card.py            # 四套模板：干员卡、装置页、子页面、公招
    ├── render.py          # 本地 Playwright 渲染 + 远程回退
    ├── ocr.py             # 本地 OCR（公招截图）
    ├── arknights.py       # 明日方舟：torappu.prts.wiki
    └── endfield.py        # 终末地：data.akedata.wiki
```

## 干员卡

版式取自 [AmiyaBot operatorArchives](https://github.com/AmiyaBot/Amiya-Bot-plugins/tree/e7cf5a426cfd2458f3b3e376734d79779fe77da2/src/arknights/operatorArchives) 的 `operatorInfo.html` / `operatorInfo.css`，样式逐条照搬；底纹、职业图标、技能图标、潜能徽章、基建徽章与立绘全部内联，渲染时不回源拉图。

| 区块 | 字段 | 来源 |
| --- | --- | --- |
| 表头 | 名称 / 英文名 / 星级 / 职业 - 子职业 / 站位 / 标签 | `character_table`、`uniequip_table.subProfDict`、`tagList`；异格干员额外打紫底标签 |
| 数值 | 最大生命值 / 攻击力 / 防御力 / 魔法抗性 / 攻击速度 / 攻击间隔 / 阻挡数 / 部署费用 / 再部署时间 | 满级取 `phases[-1].attributesKeyFrames[-1].data`，信赖取 `favorKeyFrames[-1].data`，显示为 `基础+加成` |
| 档案 | 势力 / 阵营 / 队伍 / 种族 / 画师 / 生日 / 真名 / CV | `handbook_team_table`、`handbook_info_table`（`【键】值`）、`skin_table`、`charword_table`；缺值打 `未知` |
| 潜能提升 | 序号 + 描述 | `potentialRanks[*].description` |
| 攻击范围 | `■` 网格 | `range_table[rangeId].grids` |
| 特性 | 一行 | `character_table[<id>].description` |
| 干员印象 | 两行 | `itemUsage` / `itemDesc` |
| 信物 | 名称 + 描述 | `potentialItemId` → `item_table.items` |
| 天赋 | 名称 + 描述 | `talents[*].candidates[-1]`，`blackboard` 填占位符 |
| 基建技能 | 精英阶段 + 徽章 + 名称 + 描述 | `building_data.chars[].buffChar` → `buffs` |
| 技能 | 名称 / 回复类型 / 触发类型 / 技力 / 持续 / 描述 / 范围 | `skill_table[<skillId>].levels[-1]` |

描述里的 `<@ba.kw>` 一类富文本会剥掉，被标记的片段转成 `<span class="mark">` 按模板的 `#00b3ff` 高亮；`{atk:0%}` 一类占位符用该技能 `blackboard` 填充，百分比按四位有效数字渲染。1★ 小车本身没有技能，卡片只有表头与数值。

## 装置页

装置单独出图，版式取自 `operatorToken.html`：1280×720、深色底图、标题为「{干员名} 的召唤物」。

| 区块 | 来源 |
| --- | --- |
| 标题头像 / 装置头像 | `raw/char_avatar/<charId>.png`、`raw/char_avatar/<tokenId>.png` |
| 名称 / 英文名 / 站位 | `character_table[<tokenId>].name` / `.appellation` / `.position`（近战位 `#f44336`、远程位 `#4caf50`、不限部署位 `#9c27b0`） |
| 特性 | `description`，黄字 |
| 数值 8 项 | `phases[-1].attributesKeyFrames[-1].data` |
| 攻击范围 / 天赋 / 技能 | `range_table`、`talents`、`skill_table[sktok_*]`（同一技能只出一次，无描述的等级跳过） |

装置来自 `displayTokenDict`：467 名干员中 54 名有装置，共 54 页、62 个技能。没有装置的干员只发卡片。

## 子页面

干员卡之后跟一条按钮消息，五个按钮：**模组 / 皮肤 / 档案 / 阵营 / 语音**。也可以直接在群里发这五个词，插件按最近出卡的干员出图。

| 子页面 | 版式 | 来源 |
| --- | --- | --- |
| 模组 | 名称、类型、Lv1–3 加成、描述 | `uniequip_table.charEquip` → `equipDict`，加成在 `battle_equip_table[].phases[].attributeBlackboard` |
| 皮肤 | 立绘网格 + 名称 | `skin_table.charSkins[].displaySkin`，图取 `char_portrait/<portraitId>.png` |
| 档案 | 基础档案 / 客观履历 / 临床诊断分析 / 档案资料一~四 | `handbook_info_table.handbookDict[].storyTextAudio` |
| 阵营 | 同阵营干员 + 星级 | `character_table[].groupId` |
| 语音 | 按 `voiceIndex` 排序的台词 | `charword_table.charWords` |

子页面目前只支持明日方舟；终末地发这五个词不会出图。

## 材料

```
材料 固源岩
材料 炽合金
干员材料 迷迭香
```

| 指令 | 行为 |
| --- | --- |
| `材料 <名称>` | 材料档案；先按全名匹配，再按包含匹配，仍无结果时尝试当干员名，转成养成材料页 |
| `干员材料 <干员名>` / `<干员名>材料` | 养成材料清单；省略名字时用最近出过卡的干员 |
| `<材料名>` | 直接当材料查；与角色名冲突时角色优先 |
| `干员 材料` / `干员 模组` 之类 | 与不带 `干员` 前缀等价 |

材料档案的字段来源：

| 区块 | 来源 |
| --- | --- |
| 名称 / 星级 / 图标 | `item_table.items[].name` / `rarity` / `iconId` |
| 简介 / 用途 | `description` / `usage` |
| 掉落关卡 | `stageDropList[].stageId` → `stage_table.stages` 的 `code`、`name`，`occPer` 转成固定掉落 / 大概率 / 概率掉落 / 小概率 / 罕见 |
| 加工 | `buildingProductList[].formulaId` |

养成材料页的字段来源：

| 区块 | 来源 |
| --- | --- |
| 精英化 | `character_table[].phases[].evolveCost` |
| 技能升级 | `character_table[].allSkillLvlup[].lvlUpCost` |
| 技能专精 | `character_table[].skills[].levelUpCostCond[].levelUpCost` |
| 模组 | `uniequip_table.equipDict[].itemCost`（只取 `type == ADVANCED`） |

理智效率与一图流统计尚未接入。

## 技能

```
技能 迷迭香
迷迭香技能
干员技能 迷迭香
技能 思维膨大
强力击·β型
```

| 指令 | 行为 |
| --- | --- |
| `技能 <名称>` / `<名称>技能` | 先按干员名解释，找不到再按技能名 |
| `干员技能 <干员名>` | 同上，强制按干员解释 |
| `<技能名>` | 单词语依次尝试角色、材料、技能 |

技能页字段来源：

| 区块 | 来源 |
| --- | --- |
| 基本信息 | `skill_table[].levels[-1]` 的 `spData`（回复方式、技力）、`skillType`（触发方式）、`duration` |
| 描述 | 同一等级的 `description`，占位符用 `blackboard` 填充 |
| 专精变化 | `levels[7:]` 的描述，按专精一、二、三排列 |
| 拥有者 | `character_table[].skills[].skillId` 反查，同名技能列出全部干员 |

同名技能（如 `强力击·β型` 有 7 名干员携带）列出拥有者与效果，不硬猜是哪一个。

## 公招

```
公招 资深 输出
公开招募 控场 减速
高资 治疗
高姿 输出
```

触发词四个都行：`公招`、`公开招募`、`高资`、`高姿`。其中 `高资`、`高姿` 同时就是「高级资深干员」这个标签，后面不带别的标签也能直接查。

- 标签：职业、位置、特性，以及 `新手` / `资深干员` / `高级资深干员`；`资深`、`高资`、`高级资深` 会自动补全。
- 候选池取自 `gacha_table.recruitDetail`（当前 163 人），不是「所有可获得干员」——限定干员不进公招。
- 组合取 1–3 元，剔除 `高级资深干员` 与 `资深干员` 同现的组，按「标签数降序、最高星级降序」排列。
- 判定照搬源插件：标签按字符串拼接后用子串包含判定；6★ 要求组合含「高级资深干员」；只收稀有度 ≥ 4（或 1★）；遇到首个 3★ 即 `break`，该组整体丢弃。
- 页面照搬 `operatorRecruit.html`：每组一列、左侧 5px 竖条、标签胶囊、60px 半身像、底边按星级着色；不设标题、不截断。
- 无结果时回文本，例如「博士，没有找到可以锁定稀有干员的组合」。

**截图识别**：只发「公招」时，插件会等你发一张界面截图（30 秒内），用本地 `rapidocr-onnxruntime`（纯 CPU、无需 API key）识别文字，再用最长优先匹配标签词表。一张图读不出标签会继续等下一张，识别成功即出图。

## 终末地

终末地走「档案」版式：左侧数值与档案，右侧立绘。

| 区块 | 来源 |
| --- | --- |
| 数值 | `CharacterTable[].attributes[-1]` 的 `attrType` 1、2（生命值、攻击力） |
| 属性 | 同帧 `attrType` 39–42（力量、敏捷、智识、意志） |
| 档案 | `profileRecord[].recordDesc` 的「基础档案」；职业取 `CharProfessionTable`，主/副能力取 `mainAttrType` / `subAttrType`，武器取 `defaultWeaponId` → `ItemTable` |
| 潜能提升 | `CharacterPotentialTable[].potentialUnlockBundle[].name` |
| CV | `cvName` 四个 id → `I18nTextTable_CN` |

不输出防御力（`attrType` 3 在全部 35 名角色上都是 0，打印等于编造），也不输出画师、专长、爱好（公开数据没有中文文本源）。信赖、模组、技能、装置在终末地侧尚未接入。

## 渲染

优先用**本地无头 Chromium**（Playwright），并把 `HarmonyOS_Sans_SC.ttf` 放在文档同目录以相对路径引用，字形与模板一致。本地不可用时回退 AstrBot 的文转图服务（那条路径没有该字体，会退回系统字体栈）。

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

插件会在自身数据目录下找 `PLAYWRIGHT_BROWSERS_PATH` 指向的浏览器；安装到插件目录时无需再设环境变量：

```bash
PLAYWRIGHT_BROWSERS_PATH=<插件数据目录>/browsers python -m playwright install chromium
```

渲染失败就不回复，不回退成文本。成品按角色或标签组合缓存：

| 产物 | 缓存名 |
| --- | --- |
| 干员卡 | `card_v16_<game>_<id>.png` |
| 装置页 | `token_v2_<game>_<id>.png` |
| 子页面 | `panel_v1_<game>_<op>_<id>.png` |
| 公招 | `recruit_<标签哈希>.png` |

改动版式后递增对应前缀即可让旧图失效；换版式时务必一并删掉同名老缓存。

## 缓存

数据与图片分两级缓存，目录 `data/plugin_data/astrbot_plugin_rhodes_island_archive/cache/`：内存里保留 `cache_ttl` 秒，超过后落盘副本直接复用；磁盘副本过期时先用一次 `HEAD` 校验，内容没变就继续用，变了才重新下载。清理时删掉该目录即可。

## 已知限制

- 一次只查一个角色：带空格或含 `/` 的消息不参与匹配。
- 子页面（模组 / 皮肤 / 档案 / 阵营 / 语音）目前只支持明日方舟。
- 终末地只输出满级数值与档案，没有信赖、模组、技能与装置。
- 首次使用需要装一次 Chromium，否则只回文本提示。

## 待办

- [ ] 终末地的子页面（模组 / 档案 / 语音）
- [ ] `views/` 可视化页面

## 鸣谢

- [AmiyaBot / Amiya-Bot-plugins](https://github.com/AmiyaBot/Amiya-Bot-plugins)：干员卡、装置页、公招页的模板与招募算法来源
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：插件框架、消息平台适配与文转图服务
- [torappu.prts.wiki](https://torappu.prts.wiki)：明日方舟解包数据与资源镜像
- [data.akedata.wiki](https://data.akedata.wiki)：终末地数据与静态资源镜像

以上项目的作者与维护者让这个插件成为可能，数据与素材的版权归原作者及各游戏版权方所有。

## 许可证

[MIT](LICENSE)

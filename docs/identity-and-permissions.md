# 身份与权限：Content Object 模型、Person/Account/Device、隔离架构

**这份文档是什么**：把「家里」的权限、信息/记忆/文件隔离架构定下来——统一的内容权限模型、Person/Account/Device 三层身份拆分、创建/加入家庭在服务端对应什么状态变化。**这份文档只做设计，不改代码**——`omni/memory.py`、`omni/server.py` 等現有实现不受这次改动，具体实现留到下一个会话。

**触发原因**：`family-app-architecture.md` §5.3 留了一处没有定论的矛盾（Feed 默认展示 vs 分享默认不共享），`todo.md`（客户端仓库）P3 也把"四层隐私可见性只有雏形、没有客户端设计"列为待办。这份文档把两边都接上，一次定清楚。

**和 `family-app-architecture.md` 的关系**：不是重写，是**深化**。该文档 §3 已经定了 Photo/Story/Suggestion 三个新实体不进 `MemoryEntry`；这份文档回答的是下一层问题——这些实体（以及 `MemoryEntry` 本身）应该长什么样的权限字段、`scope` 这个字符串背后到底是什么身份模型、§5.3 的矛盾具体怎么收尾。读这份之前建议先读 `family-app-architecture.md` §3-4-5。

**和 `omni`（客户端）仓库的关系**：客户端那边有一份姊妹文档 `docs/onboarding-and-identity-flow.md`，讲的是同一个模型在客户端的样子——App 级状态机、创建/加入家庭的 UI 流程、两种不同的"魔法三十秒"。两份文档互相引用，不重复内容：这份讲**数据长什么样、谁能看**，客户端那份讲**用户怎么一步步走到这个状态**。

---

## 1. 结论先行

- **不新起一套模型，扩展 `MemoryEntry` 已有的字段。** `scope`、`source.origin_scope`、`source.speaker_id`、`source.confidence`、`written_by`——这五个字段已经覆盖了「谁的、从哪来的、AI 有多确定、谁写的」。真正缺的只有两块：**`visibility` 要从 `scope` 里独立出来**（现在 `scope` 同时承担"归属"和"谁能看"两个职责，这在需要"只给部分家庭成员看"的场景下不够用），以及一个新的 **`ai_usage`** 字段（现在没有办法表达"这条内容我自己的 AI 也不要用"）。第 2 节展开。
- **设计需求文档 §19 的"四层"其实是两个维度，不是一个梯子。** Private/Family Shared/Selected Members 是"谁能看"（`visibility`），Public/External Share 是完全独立的另一个维度（"能不能被主动带出家门"，`share_policy`）。这不是推翻 §19，是把它翻译成可以校验的两个字段——第 3 节详细说明为什么混在一个梯子里会出问题。
- **`family-app-architecture.md` §5.3 的矛盾，这份文档给出解法：方案 C（确认后才进家庭空间），并且给出了让它不显得啰嗦的具体机制**——不是每次都弹窗问，是低摩擦的、有记忆的确认。第 6 节。
- **Person ≠ Account ≠ Device。** 一个人可以先存在于家庭里（有名字、有头像、有关系），完全没有账号、没有设备绑定——这是老人和小孩能被"带进家"而不是自己"注册"的前提。第 4 节。
- **一个人可以让 AI 深度了解自己，同时对家人完全不可见——这条现有架构已经天然满足，不用新加机制。** `disclosable()` 按*这一轮输出去哪*判断，不是按*AI 知道什么*判断，私人 `user:` 作用域的内容从来不会被拿去支撑一个 `group:family` 输出。第 5 节把这点讲清楚，顺带说明"连接引擎不能挖私人内容当弹药"这条防线为什么在写入侧就该拦住，不能只指望这层判断。

---

## 2. Content Object：统一权限字段，但不统一存储表

### 2.1 现有字段 → 权限概念的映射

`MemoryEntry` 已经有的字段，直接对应权限模型里最重要的几个概念，不用重新发明：

| 权限概念 | 现有字段 | 状态 |
|---|---|---|
| 归属者是谁 | `scope`（`user:<id>` / `group:family_<id>`） | ✅ 已有，但承担了两个职责，见第 3 节 |
| 从哪来的 | `source.origin_scope` | ✅ 已有——这是防"洗白通道"的关键字段（`memory-design.md` §4） |
| 谁说的 | `source.speaker_id` | ✅ 已有 |
| AI 有多确定 | `source.confidence` | ✅ 已有 |
| 谁写的（人 / AI 提炼） | `written_by` | ✅ 已有，且是 `persona`/`policy` 层安全边界的执行点（`memory-design.md` §5） |
| 谁能看 | —— | ❌ 缺，见第 3 节 |
| AI 能不能用 | —— | ❌ 缺，见第 5 节 |
| 能不能被带出家门（分享到微信等） | —— | ❌ 缺，见第 3 节 |

### 2.2 Photo / Story / Suggestion 也要有同一套字段，但不进同一张表

`family-app-architecture.md` §3 已经定了这三个实体各自建表、不塞进 `MemoryEntry`——这个决定不变。这份文档加的是：**它们的权限字段集合要和 `MemoryEntry` 完全一致**，哪怕物理上是不同的列。也就是说，`photos`/`stories`/`suggestions`/`memory_entries` 四张表，每张都独立包含：

```
owner_scope       -- 复用现有的 scope 概念（谁的）
origin_scope       -- 复用现有概念（从哪来）
visibility         -- 新增，见第 3 节
allowed_members[]   -- 新增，仅 visibility=SELECTED 时使用
ai_usage           -- 新增，见第 5 节
share_policy       -- 新增，见第 3 节
confidence         -- 复用现有概念
```

**不做成一张统一的 `content_objects` 表 + 各实体外键关联。** 理由和 §3 保留分表的理由一致：这四类内容的其余字段（Photo 的 `storage_key`、Story 的 `narrative`、Suggestion 的 `state` 生命周期）差异很大，硬塞进一张多态表会让每次查询都要处理一堆跟当前实体无关的 NULL 列。统一的是**字段集合的定义和校验逻辑**（下面的 `CanRead`/`CanAIUse`/`CanShare` 三个函数只写一遍，四张表共用），不是物理表结构。

---

## 3. `visibility` 从 `scope` 里独立出来

### 3.1 现在的问题

`scope` 现在同时回答两个问题：「这条内容算谁的」和「谁能看到它」。对 `user:<id>` 一直没问题（本人的就只有本人能看），但对 `group:family_<id>` 就不够用了——family-app-architecture.md 自己在讨论"老人的私聊和家庭共享空间"时已经暗含了一个 `scope` 表达不出的场景：**指定给某几个家庭成员看，不是全家，也不是私人**。设计需求文档 §19 的 "Selected Members" 层，现在的模型完全没有落点。

### 3.2 拆分方案

```
owner_scope: "user:<person_id>" | "group:family_<family_id>"
visibility:  PRIVATE | FAMILY | SELECTED
allowed_members: [person_id]  -- 仅 visibility=SELECTED 时非空
```

`owner_scope` 回答"这是谁的东西"（决定它算在谁的存储配额里、谁能编辑/删除它）；`visibility` 回答"谁能看"。两者大多数时候一致（`owner_scope=user:X` 的东西默认 `visibility=PRIVATE`），但不永远一致——`owner_scope=user:X, visibility=SELECTED, allowed_members=[Y]` 就是"这是我的东西，但我指定给 Y 看"，这正是设计需求文档 §19 "Selected Members" 想表达的场景，也是 ChatGPT 讨论稿里"女儿→妈妈：这是给你的"这个例子。

### 3.3 `share_policy` 是另一个维度，不是第四层

设计需求文档 §19 把 Public/External Share 列成第四层，容易让人以为它是"比 Family 更公开"的下一级。实际上它是**完全独立的一个开关**：

```
share_policy: NOT_ALLOWED | ALLOWED
```

`visibility=FAMILY` 决定"家里谁能看"，`share_policy` 决定"这条内容能不能被主动带出家门（分享到微信朋友圈等）"——两者正交。一条 `visibility=FAMILY, share_policy=NOT_ALLOWED` 的内容，全家能看，但没人能把它分享出去（比如涉及某个家庭成员隐私细节的东西）；一条 `visibility=PRIVATE, share_policy=ALLOWED` 理论上也说得通（我自己的东西，我自己想分享出去）。

**`share_policy=ALLOWED` 不等于自动分享。** 家里的产品哲学第一条就是"AI 是促成者，不是替代者"——分享永远要走一次用户主动确认（`family-app-architecture.md` §8 已经把这条原则用在"AI 起草话术，本人确认后自己发"上；这里是同一条原则用在内容分享上）。`share_policy` 只决定分享入口存不存在，从不决定分享会不会发生。

**这不是推翻设计需求文档 §19，是把它翻译成可校验的字段。** 建议 §19 未来修订时把措辞从"四层"改成"三层可见性 + 一个独立的分享开关"，但这是产品文档，不是这份文档能替你改的，这里只提出这个建议。

### 3.4 判断函数

```
CanRead(reader_person_id, object):
    if reader_person_id == owner_person(object.owner_scope):
        return True
    if object.visibility == FAMILY:
        return reader_person_id in current_members(family_of(object.owner_scope))
    if object.visibility == SELECTED:
        return reader_person_id in object.allowed_members
    return False

CanShareExternally(object, requesting_person_id):
    return object.share_policy == ALLOWED
        and requesting_person_id == owner_person(object.owner_scope)
        # 且必须经过一次用户主动确认动作，这一步不由函数本身决定，
        # 是调用方（客户端的分享确认页）的职责
```

`current_members(...)` 现算，不用快照——这是 `memory-design.md` §4 已经定的原则（退群立刻失效，不能有静态汇总）在这里的延伸，直接复用同一条纪律。

---

## 4. Person ≠ Account ≠ Device

### 4.1 为什么要拆开

老人可能没有手机号、没有邮箱、不会注册；小孩可能有 iPad 但没有手机号。如果"成为家庭成员"的前提是"先注册一个账号"，这两类人就永远进不了这个产品的核心场景。ChatGPT 讨论稿里这条判断是对的：**先有家人，再有账号**——这不是权宜之计，是这个产品服务"三代人"这个定位下的必然要求。

拆成三个独立概念：

```
Person   -- 真实的人。可以没有 Account，可以没有 Device。
Account  -- 登录身份（目前设计是微信 OAuth，见 family-app-architecture.md §8）。
Device   -- 这次会话绑在哪个浏览器/设备上（web app，见第 4.4 节，不是原生设备标识）。
```

一个 Person：
- 一定属于一个 Family（V1.0 不支持一人多家庭，见第 4.2 节）。
- 可能有 Account，也可能没有（`account_id` 可空）。
- 当前会话可能绑在某个 Device 上，也可能完全没有设备记录（一个刚被创建、还没被激活的 Person）。

### 4.2 数据模型

```
families
  id            TEXT PK
  created_by    TEXT REFERENCES persons(id)  -- Creator，见第 4.3 节
  created_at    REAL

persons
  id              TEXT PK
  family_id       TEXT REFERENCES families(id)
  display_name    TEXT
  avatar_url      TEXT NULL
  relationship    TEXT   -- 相对于 Creator 的关系标签，如"妈妈"/"女儿"，自由文本，不是枚举
                          -- （家庭关系词汇远比"elder/parent/child"三分丰富，
                          --   角色分组用于默认视图选择，见客户端文档；这个字段是给人看的称呼）
  role_group      TEXT   -- elder | parent | child —— 决定默认落点（"三扇门"），
                          -- 是 relationship 的粗分类，不是替代
  account_id      TEXT NULL REFERENCES accounts(id)
  created_by      TEXT REFERENCES persons(id)  -- 谁创建了这条 Person 记录（自己 or 家人代创建）
  created_at      REAL
  onboarded_at    REAL NULL   -- 完成魔法三十秒的时间戳；NULL = 还在 onboarding 中
                                -- （客户端文档 onboarding-and-identity-flow.md §2.2 需要这个字段
                                --   支撑"中途关闭浏览器，重新打开时该恢复到哪一步"的判断）

accounts
  id            TEXT PK
  auth_method   TEXT      -- wechat | magic_code（见客户端文档的邀请机制）
  wechat_openid TEXT UNIQUE NULL
  wechat_unionid TEXT NULL
  created_at    REAL

devices
  id                TEXT PK
  person_id         TEXT REFERENCES persons(id)   -- 当前绑定给谁；可以改绑（见 4.4）
  session_token_hash TEXT UNIQUE
  platform          TEXT   -- 浏览器 UA 摘要，非强身份
  last_active_at    REAL
  created_at        REAL
```

`persons.family_id` 是**外键，不是数组**——一个 Person 精确属于一个 Family。这是 V1.0 的产品决定（ChatGPT 讨论稿里已经论证过："一个用户一个 JIA 家庭"，不做小家/大家），跟 `MemoryEntry` 的 `scope`/`disclosable()` 机制**不冲突**：那套机制在设计时就支持一个人拥有多个 `group:family_*` 成员关系（`memberships` 是现算的列表，不是写死的单值，见 `family-app-architecture.md` §4 最后一段），这里只是**产品业务规则**收紧成"当前只允许一个"，机制本身没有被削弱——以后要开放多家庭，加的是业务校验，不是重新设计存储。

### 4.3 Creator 和普通 Person 没有权限差异，只有一个例外

ChatGPT 讨论稿这条判断是对的，直接采纳：**不做"家庭管理员"**。`families.created_by` 只记录"这个家是谁建的"，唯一对应的特殊权限是：

- 删除整个 Family（级联使当前所有 `group:family_<id>` 内容的 `visibility` 立即失效——具体走 `memory-design.md` 已有的 `forget_origin` 机制，把 `origin_scope=group:family_<id>` 的内容全部标记过期）。
- 移除某个 Person（见第 4.5 节，级联规则跟这条一致）。

除此之外，每个 Person 对自己拥有的内容（`owner_scope=user:<自己的id>`）有完全的读写权，对 `visibility=FAMILY` 的内容有读权——Creator 不比其他家庭成员多任何一点这类权限。这避免了"家庭群管理员"那种科层感，跟产品哲学"每个人都该有被请教的时刻，不只是被管理"一致（CLAUDE.md 核心哲学第 2 条）。

### 4.4 Device：web app 语境下的含义

**产品形态是手机浏览器 Web App，不是原生 App**（`omni` 仓库 CLAUDE.md 已经明确）。这意味着 ChatGPT 讨论稿里"设备配对"、"Nearby Pairing（类似 AirDrop）"这类依赖原生系统能力（蓝牙、系统级设备发现）的机制**在当前形态下不可用**——Web App 拿不到这些 API。`devices` 表里的"设备"实际上是：

> **一个持久化在浏览器里的会话凭证**（`session_token_hash`，存在 `localStorage`），不是操作系统级的设备身份。

这带来一条硬约束，直接影响客户端文档要设计的邀请机制：**"设备配对"只能通过"在同一台设备的浏览器里打开一个链接/输入一个码"实现，不能是两台设备之间的近场直连。** 具体的邀请流程设计（哪几种方式、老人怎么被"帮着加入"）留给客户端文档的第 4 节，这里只定服务端需要支撑的能力边界：

- 服务端要能签发一个「面向某个 Person、有时效」的邀请凭证（可以是短链接携带的 token，也可以是人类可读的 6 位 Magic Code——两者本质是同一个 token 的两种编码），扫码/输入这个凭证的浏览器换取一个绑定到该 Person 的 `session_token`。
- 换取动作必须是**在最终要绑定的那台设备的浏览器里完成的**（凭证本身可以通过任何渠道传递——微信分享链接、口头念一个 6 位数字——但兑换必须发生在目标浏览器上），不能是"设备 A 确认设备 B 加入"这种需要两端实时握手的模式。

### 4.5 Person 离开家庭

复用第 4.3 节提到的 `forget_origin` 机制，规则跟 ChatGPT 讨论稿一致，且和已有架构完全吻合，不用新设计：

- Person 自己 `owner_scope=user:<id>` 的内容（私人记忆、私人照片）：不受影响，因为它们的 `origin_scope` 本来就是这个人自己，`forget_origin` 只处理"这个作用域产生的内容在别处的痕迹"，不处理这个作用域自己的东西。
- 这个人贡献进 `group:family_<id>` 的内容（`origin_scope=group:family_<id>` 或者 `origin_scope=user:<该人id>` 但被提升进了家庭空间）：按 `forget_origin(family_id)` 还是 `forget_origin(该人的 user scope)` 取决于具体是谁的产出——**这里的原则是"家庭 Memory 不等于家庭成员个人数据的共同财产"**（ChatGPT 讨论稿的措辞很准确），落到实现就是：只有明确以 `group:family_<id>` 为 `origin_scope` 写入的内容才算"家庭共同财产"，继续留存；以某个人为 `origin_scope`、只是 `visibility=FAMILY` 的内容，那个人离开时应该级联失效。
- **这条规则要求 `origin_scope` 在写入时就分清楚"这是家庭共同产出"还是"这是某个人的东西，只是给家人看"**——提炼流程（`Extractor`）和未来的 Story/Suggestion 生成流程在决定 `origin_scope` 时要把这个区分当成硬性要求，不能图省事全部写成 `group:family_<id>`。

---

## 5. `ai_usage`：AI 能不能用，和人能不能看，是两回事

### 5.1 现有架构已经满足的部分

`disclosable()` 判断的是"这一轮*输出*去哪"，不是"AI 知不知道"——这个设计从一开始就把两件事分开了（`memory-design.md` §4）。具体到 ChatGPT 讨论稿举的例子：

> 妈妈跟 AI 说"我最近有点担心身体"，女儿问 AI"妈妈最近怎么样"，AI 不应该说出妈妈的原话。

现有架构下这自动成立：妈妈这句话如果被提炼进 `MemoryEntry`，`owner_scope` 是 `user:<妈妈>`，`visibility=PRIVATE`（或者默认值，见第 6 节）。女儿的这轮对话，`output_scope` 是女儿自己的 `user:<女儿>` 作用域。`disclosable()` 检查"这条内容的 `owner_scope` 是不是等于 `output_scope`，或者 `visibility` 允不允许"——都不满足，这条内容从一开始就不会被检索出来喂给回答女儿的那次对话。**不需要额外加一条"AI 不能说"的规则去弥补，结构上它根本不会被看到。**

第 4.3 节提到的连接引擎同理：`family-app-architecture.md` §5.2 已经把"连接引擎只检索 `group:family` 作用域内容，从不检索 `user:` 私人作用域"定成了写入侧的硬规则，不是运行时过滤——跟这里是同一条防线原则的两次应用。

### 5.2 真正缺的：一条"不要记住这个"的开关

现有机制能做到"不给别人看"，但做不到"连我自己的 AI 都不要记住"——`ephemeral` 层能做到"这次会话内有效、会话结束就丢"，但如果用户想要的是"这句话现在说出来，但完全不要进入长期记忆，哪怕是只有我自己能看到的那种"，现在没有对应的机制。

新增 `ai_usage`：

```
ai_usage: PERSONAL | FAMILY_CONTEXT | NONE
```

- `PERSONAL`（默认）：可以被这个人自己的 AI 会话使用（检索、注入）。
- `FAMILY_CONTEXT`：额外允许被"家庭级别"的 AI 推理使用——目前架构下这个值主要是为将来预留（现在的 Sidecar/连接引擎已经通过 `owner_scope`/`origin_scope` 是不是 `group:family_*` 来判断，`ai_usage=FAMILY_CONTEXT` 是在此基础上的显式声明，双重校验：内容要同时"归属或来源于家庭作用域"且"没有被显式排除出家庭级推理"，两个条件都满足才用）。
- `NONE`：**不进入任何长期记忆**，包括这个人自己的。这是"我知道你会用这个了解我，但这句话例外"的表达。

用户界面上不需要出现这三个词（ChatGPT 讨论稿这条建议是对的）——客户端只需要暴露"让 AI 记住"/"这句不用记"这类自然语言级别的开关，具体值域是服务端内部概念。

### 5.3 落到提炼流程：`ai_usage=NONE` 必须在写入前拦截，不是写入后标记

这条纪律很重要：`ai_usage=NONE` 的判定必须发生在 `Extractor` 决定"要不要从这段对话里提炼出一条 `MemoryEntry`"这一步之前，而不是先提炼出来、再打上 `ai_usage=NONE` 的标签然后指望后续检索跳过它。原因和第 5.1 节的原则一致：**防线要建在生成侧，不能只指望使用侧的判断**——一条已经写入库里、只是标了"别用"的记录，永远比"从来没被写入"多一层"万一某处漏判了这个标志"的风险。用户说"这段不要记"，实现上应该是这段对话内容根本不进入 `Extractor` 的输入，而不是进入之后被标记排除。

---

## 6. `family-app-architecture.md` §5.3 矛盾的解法

### 6.1 回顾矛盾

价值观第 3 条和 V2 路线图都说"分享永远是主动选择，不因为同一家庭空间被默认共享"；但 MVP 的 Family Feed 效果图里，"爸爸今天跟 AI 聊起了年轻时第一次买车"直接出现在家庭 Feed 里，没有经过任何确认动作。

### 6.2 解法：默认 `visibility=PRIVATE`，Feed 只呈现"已确认"和"低摩擦确认后"的内容

结合这份文档已经定的模型，具体规则：

1. **`Extractor` 提炼出的每一条 `MemoryEntry`，默认 `owner_scope=user:<说话人>`，`visibility=PRIVATE`。** 不管这句话听起来多么适合放进 Family Feed，写入时永远先落私人作用域——这是"分享永远是主动选择"这条价值观在数据模型层面的字面体现，不是口号。
2. **`Extractor` 额外判断"这条内容是否值得建议提升到家庭空间"**，产出一个独立的布尔位（不是直接改 `visibility`）——复用 CLAUDE.md 已经定义的 "Family Candidate" 概念（Memory Decision Engine 的四态之一：不保存 / Private Memory / Family Candidate / Family Memory）。这条判断本来就该做，跟本文档新增的字段没有冲突，是同一套决策的产出。
3. **`Family Candidate` 呈现给本人（不是直接呈现给全家）一次低摩擦确认**："要不要让家人也看到你提到的这件事？"——确认动作把这一条（不是这段对话的全部内容，只是提炼出的这一句摘要）的 `visibility` 改成 `FAMILY`，同时 `owner_scope` 保持不变（内容还是这个人的，只是可见性变了）。
4. **拒绝或者没有回应的 `Family Candidate` 保持 `PRIVATE`，不重复打扰**——同一条内容不会被反复询问，`Extractor` 需要有去重判断（跟已有的 supersede/冲突检测是同一类问题，复用 `memory-design.md` §6 的机制思路）。

这就是 `family-app-architecture.md` §5.3 里建议的"方案 C"，这里给出了具体机制：**确认不是每次弹一个阻断式对话框，是一条出现在本人自己视野里、可以随手滑过或者点一下的轻量提示**（具体 UI 形态是客户端的事，见姊妹文档）。第一版 Family Feed 效果图里那条"爸爸的回忆"要出现，前提是爸爸自己已经点过一次确认——**这意味着 MVP 早期、家庭刚建立、还没人做过确认动作时，Feed 大概率是稀疏的**，这是符合"分享是主动选择"这条原则的真实代价，不是要去掩盖的缺陷。

### 6.3 什么内容可以跳过确认，直接进 Family Shared

不是所有内容都要走确认——**用户主动上传到家庭相册的照片、主动对着"今天的家"页面发的内容，本来就是主动分享的动作本身**，不需要"分享后再问一遍要不要分享"这种多余的摩擦。区分规则：

> **凡是用户在私人语境（跟 AI 一对一对话、跟我说说话）里说的话，`Extractor` 提炼出的内容默认私人，需要确认才能提升。凡是用户直接对着家庭共享界面做的动作（上传家庭相册、在 Family Feed 里发内容），这个动作本身就是分享确认，不需要二次确认。**

这条区分线跟"信息是怎么产生的"直接挂钩，客户端文档需要在 UI 层面让这条线对用户是直觉的（跟我说说话 = 私密，今天的家 = 共享），不需要每次操作前解释。

---

## 7. 对 `family-app-architecture.md` 的具体修订点

| 章节 | 修订 |
|---|---|
| §3 草案字段 | `photos`/`stories`/`suggestions` 三张表补上本文档第 2.2 节定义的权限字段集合（`visibility`/`allowed_members`/`ai_usage`/`share_policy`），原有字段不变 |
| §4 Family Graph | `family_members` 表被本文档第 4.2 节的 `persons` + `families` 取代（`persons` 本身携带家庭归属，不需要单独的关联表）；`role` 字段改名 `role_group`，含义不变 |
| §5.3 | 本文档第 6 节给出确定解法，§5.3 的"需要你来定"状态解除 |
| §8 身份 | 本文档第 4 节是 §8 的完整展开，`users`/`credentials` 两张草案表被 `persons`/`accounts`/`devices` 取代 |

---

## 8. 开放问题

1. **`role_group` 的粗分类（elder/parent/child）在关系很复杂的家庭里可能不够用**（比如二婚家庭、多代同堂）——这不阻塞 MVP（`relationship` 自由文本字段已经能覆盖大多数称呼需求，`role_group` 只影响"默认落点选哪扇门"这一件事，选错了用户可以自己切换），但值得记录，以后可能需要更细的角色分级。
2. **`ai_usage=FAMILY_CONTEXT` 目前只是预留字段，还没有一个真实的"家庭级别 AI 推理"消费它**——连接引擎现在是靠 `owner_scope`/`origin_scope` 本身是不是家庭作用域来判断，这个字段暂时是双重保险而不是唯一判据。等真的出现"需要显式声明才能被家庭级别使用"的场景（比如以后某条个人内容想主动开放给连接引擎，但本身归属还是私人）再激活它的实际语义。
3. **微信 openid 变更/换绑的处理**没有设计——用户换手机号、微信账号异常等场景下 `accounts.wechat_openid` 怎么迁移，这个留给身份体系真正实现时处理，不是 V1.0 的架构问题。

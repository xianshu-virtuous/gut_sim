"""肠胃模拟插件配置。

配置文件默认路径：config/plugins/gut_sim/config.toml

设计原则：
- 默认是一个安静的模拟器：只推进两条数值、只在二档以上注入一句感受。
- 所有"猎奇"部分（失禁 / 羞耻干扰 / 强制短句）都各自独立开关，用户自己往上开。
- 数值全在这里可调，不写死在代码里。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section

#: 默认注意不到的档位阈值（绝对点，与上限无关）
_TIER_1_DEFAULT = 60.0
_TIER_2_DEFAULT = 90.0


class PluginSection(SectionBase):
    """插件基础设置。"""

    enabled: bool = Field(default=True, description="是否启用插件")
    debug_log: bool = Field(
        default=False,
        description="是否在日志里输出每轮数值变化、事件判定与注入细节",
    )
    store_name: str = Field(
        default="gut_sim",
        description="状态持久化用的 storage 名（data/json_storage/gut_sim/*.json）",
    )
    allow_group: bool = Field(
        default=True,
        description=(
            "是否在群聊里也推进数值。关掉后只在私聊模拟，群里完全不动。"
            "（无论开不开，群里默认不注入猎奇档内容）"
        ),
    )
    group_inject_tier: int = Field(
        default=3,
        description=(
            "群聊里最低注入到第几档。默认 3＝群里只在「强烈」时才注入一句，"
            "避免群聊被身体细节刷屏；填 4 表示群里完全不注入。"
        ),
    )


class NumericSection(SectionBase):
    """两条数值的推进规则。"""

    cap_init: float = Field(
        default=100.0,
        description="两条的初始上限（屁意 / 便意各自独立，初始相同）",
    )
    cap_max: float = Field(
        default=300.0,
        description="靠憋忍经验能成长到的上限（不含奖励加成）",
    )
    cap_min: float = Field(
        default=60.0,
        description="肠胃弱化最多把上限压到这个值，防止被练废",
    )
    base_rate: float = Field(
        default=1.5,
        description="便意每轮的基础增量（会再乘人设推断出的倍率）",
    )
    gas_ratio: float = Field(
        default=2.5,
        description=(
            "屁意相对便意的增速倍率（2.0~3.0 之间）。\n"
            "插件初始化时会按人设推断一个值写进档案；这里是被配置文件覆盖时的默认。"
        ),
    )
    token_div: float = Field(
        default=250.0,
        description=(
            "token 加成除数：本轮（用户消息 + 回复）的 token 总量除以它，加到便意上。\n"
            "填 0 表示只按轮次推进，不看 token。"
        ),
    )
    token_cap_per_round: float = Field(
        default=5.0,
        description="单轮 token 加成最多折合多少点（防长文/重工具链把数值推爆）",
    )
    tier_1: float = Field(
        default=_TIER_1_DEFAULT,
        description="二档（有感）的下限。0 ~ 这个数是无感",
    )
    tier_2: float = Field(
        default=_TIER_2_DEFAULT,
        description="三档（强烈）的下限。超过这个数进入强烈档",
    )
    decay_per_hour: float = Field(
        default=0.0,
        description=(
            "两条数值的自然衰减（点/小时）。默认 0＝不会自己好转，"
            "填正数后长时间不说话会回落（更像肠胃会自己平息）。"
        ),
    )
    passive_vent: float = Field(
        default=0.0,
        description=(
            "自然排气：屁意到顶之后，每轮自己排掉多少点。\n"
            "默认 0＝憋着不放（封顶停住）。填 1~3 就有「气球慢慢漏气」的拟真感，"
            "而且不进失禁判定、不加羞耻。\n"
            "注意：这是被动排气，和 [shame].leak_enabled（越线失禁式漏气）是两回事。"
        ),
    )


class ToiletSection(SectionBase):
    """如厕：离线行为 + 被消息撞见时的"拦一下"。

    机制同上下文归档的巡检：安静够久就溜去洗手间，期间来了消息会被拦几秒，
    回来后如果能说（私聊白名单 / 群聊隐晦），就如实带一句"刚去了卫生间"。
    """

    # ── 手动命令 ──
    command_enabled: bool = Field(
        default=True,
        description="是否允许用 /肠胃 上厕所 手动排空（不影响自动如厕）",
    )
    relax_rounds: int = Field(
        default=2,
        description="如厕后自动获得的「舒畅」轮数（0 = 不给）。舒畅期内两条不增长",
    )
    shame_private: float = Field(
        default=0.0,
        description=(
            "私密模式下如厕时额外增加的羞耻值。\n"
            "默认 0＝不加（如厕是正常的）；想玩「被看着去厕所」那种，填正数。"
        ),
    )
    refresh_layer: bool = Field(
        default=True,
        description="如厕后是否立刻刷新注入层（否则要等下一轮 on_chatter_step 才更新）",
    )

    # ── 自动如厕（离线行为） ──
    auto_enabled: bool = Field(
        default=True,
        description="是否启用自动如厕：安静够久时自己溜去洗手间，回来把两条清空",
    )
    auto_check_seconds: int = Field(
        default=60,
        description="巡检间隔（秒）：多久检查一次「哪条流已经安静够久、该去厕所了」",
    )
    idle_seconds: int = Field(
        default=900,
        description="本流安静多少秒之后才有资格触发自动如厕（默认 15 分钟）",
    )
    chance: float = Field(
        default=0.5,
        description="每次巡检判定时的触发概率（0~1）。太高会一直在厕所里",
    )
    stay_min_seconds: int = Field(default=60, description="在洗手间待的最短秒数")
    stay_max_seconds: int = Field(default=240, description="在洗手间待的最长秒数")

    # ── 被消息撞见 ──
    hold_min_seconds: float = Field(
        default=3.0,
        description="她正在洗手间时来消息，先拦多久才处理（模拟「等一下、在忙」）",
    )
    hold_max_seconds: float = Field(
        default=4.0,
        description="拦截时长上限",
    )
    hold_chance: float = Field(
        default=0.7,
        description="被撞见时，多大比例会真的拦一下（其余当作刚好出来）",
    )
    clear_on_return: bool = Field(
        default=True,
        description="回来时把两条数值清空（真去过了，当然清空）",
    )

    # ── 作用域与措辞 ──
    scope_private: bool = Field(
        default=True,
        description="私聊里是否触发自动如厕",
    )
    private_whitelist_only: bool = Field(
        default=True,
        description=(
            "私聊是否只认白名单：开着时只有 whitelist 里的用户能撞见「她去洗手间」；\n"
            "关掉则所有私聊一视同仁"
        ),
    )
    whitelist: list[str] = Field(
        default_factory=list,
        description="私聊白名单（发消息人的 sender_id / QQ 号），每行一个",
    )
    scope_group: bool = Field(
        default=False,
        description="群聊里是否触发自动如厕（默认关：群里人多，别动不动就消失）",
    )
    group_direct: bool = Field(
        default=False,
        description=(
            "群聊里被问起时说得多直白。\n"
            "默认 False＝隐晦（「去了一下洗手间」「刚不在」）；\n"
            "True＝跟私聊一样直说上厕所"
        ),
    )
    tell_after_return: bool = Field(
        default=True,
        description="回来之后是否让角色知道「自己刚去过洗手间」（知道≠一定会说，由她自己决定要不要提）",
    )


class GrowSection(SectionBase):
    """憋忍经验与上限成长。"""

    enabled: bool = Field(default=True, description="是否启用憋忍经验成长")
    exp_per_level: int = Field(
        default=20,
        description="升一级需要多少憋忍经验",
    )
    cap_per_level: float = Field(
        default=10.0,
        description="每升一级，屁意与便意上限同步增加多少（憋忍强度同时影响屁意条）",
    )
    exp_tier2: int = Field(default=1, description="二档每轮获得多少憋忍经验")
    exp_tier3: int = Field(default=2, description="三档每轮获得多少憋忍经验")
    high_shame_multiplier: float = Field(
        default=1.5,
        description="羞耻进入高档时，憋忍经验获取倍率（四舍五入）",
    )


class ShameSection(SectionBase):
    """羞耻值：溢出容差 + 聊天干扰强度。"""

    enabled: bool = Field(default=True, description="是否启用羞耻值")
    start: float = Field(default=0.0, description="初始羞耻值")
    max: float = Field(default=100.0, description="羞耻值上限")
    band_low: float = Field(default=30.0, description="低档上限（0 ~ 这个数）")
    band_high: float = Field(default=60.0, description="中档上限（低档之上 ~ 这个数，再往上就是高档）")
    per_round_tier2: float = Field(default=1.0, description="二档每轮涨多少羞耻")
    per_round_tier3: float = Field(default=2.0, description="三档每轮涨多少羞耻")
    private_multiplier: float = Field(
        default=2.0,
        description="私密模式（检测到 sisp 处于 in_sex_mode）时羞耻增长的倍率",
    )
    decay_per_hour: float = Field(
        default=3.0,
        description="羞耻值自然衰减（点/小时），0 表示不衰减",
    )
    resist_exp_per_level: int = Field(
        default=15,
        description="抗羞耻经验：每攒多少点升一级抗性",
    )
    resist_max: int = Field(
        default=5,
        description="抗性上限：每级减少多少点羞耻增长（实际增长 = 增量 - 抗性）",
    )
    resist_exp_band: float = Field(
        default=60.0,
        description="只有羞耻值达到这个值以上，才累积抗羞耻经验",
    )

    overflow_low: float = Field(
        default=10.0,
        description="羞耻低档时，数值超过上限还能多撑多少点才失禁",
    )
    overflow_mid: float = Field(
        default=6.0,
        description="羞耻中档时的溢出容差",
    )
    overflow_high: float = Field(
        default=2.0,
        description="羞耻高档时的溢出容差",
    )

    # ── 猎奇开关（默认关，用户自己开） ──
    wetting_enabled: bool = Field(
        default=False,
        description="是否启用失禁（便意越过失禁线）。默认关：数值满到上限就停住，不会出事",
    )
    leak_enabled: bool = Field(
        default=False,
        description="是否启用漏气（屁意越过失禁线）。默认关",
    )
    shame_on_wetting: float = Field(
        default=22.0,
        description="便意失禁时一次性增加的羞耻值（漏气加得少，见 shame_on_leak）",
    )
    shame_on_leak: float = Field(
        default=4.0,
        description="漏气时一次性增加的羞耻值",
    )
    wet_cooldown_rounds: int = Field(
        default=3,
        description="失禁/漏气后多少轮内不再重复触发同类事件",
    )


class EventsSection(SectionBase):
    """4 种负面 + 3 种正面状态（由便宜模型按聊天语境判断）。"""

    enabled: bool = Field(default=True, description="是否启用状态事件系统")
    judge_task: str = Field(
        default="actor",
        description=(
            "判断用哪个模型任务（config/model.toml 的 [model_tasks.<名称>]）。\n"
            "建议指向便宜的小模型；为空时用 actor"
        ),
    )
    judge_interval_rounds: int = Field(
        default=3,
        description="每隔几轮判一次（1 = 每轮都判，最贵）",
    )
    judge_max_tokens: int = Field(default=60, description="判断请求的最大输出 token")
    judge_temperature: float = Field(default=0.2, description="判断请求的温度")
    judge_only_from_tier: int = Field(
        default=2,
        description="数值至少到第几档才值得调模型判断（默认 2，一档不浪费调用）",
    )
    judge_cooldown_rounds: int = Field(
        default=6,
        description="同一个效果触发后，多少轮内不再重复触发",
    )

    # 负面
    bloat_enabled: bool = Field(default=True, description="胀气：屁意增速 ×倍率，持续若干轮")
    bloat_multiplier: float = Field(default=2.0, description="胀气时屁意增速倍率")
    bloat_rounds: int = Field(default=3, description="胀气持续轮数")

    diarrhea_enabled: bool = Field(default=True, description="闹肚子：便意增速 ×倍率，持续若干轮")
    diarrhea_multiplier: float = Field(default=2.0, description="闹肚子时便意增速倍率")
    diarrhea_rounds: int = Field(default=3, description="闹肚子持续轮数")

    weaken_enabled: bool = Field(default=True, description="肠胃弱化：便意上限永久下降")
    weaken_amount: float = Field(default=10.0, description="肠胃弱化一次减多少上限")

    shame_burst_enabled: bool = Field(
        default=True,
        description="羞耻心爆棚（唯一随机触发、不靠模型判断）：羞耻增长倍率提升",
    )
    shame_burst_chance: float = Field(default=0.03, description="每轮触发羞耻心爆棚的概率")
    shame_burst_multiplier: float = Field(default=1.5, description="爆棚期间羞耻增长倍率")
    shame_burst_rounds: int = Field(default=3, description="羞耻心爆棚持续轮数")

    # 正面
    relief_enabled: bool = Field(default=True, description="安心：若干轮内不触发任何负面")
    relief_rounds: int = Field(default=4, description="安心持续轮数")

    strengthen_enabled: bool = Field(default=True, description="肠胃强化：便意上限 +额度（可超过成长上限）")
    strengthen_amount: float = Field(default=10.0, description="肠胃强化一次加多少上限")
    strengthen_bonus_cap: float = Field(
        default=20.0,
        description="强化带来的额外上限最多累计多少（默认 20 → 300+20=320）",
    )

    relax_enabled: bool = Field(default=True, description="舒畅：若干轮内两条数值不增长")
    relax_rounds: int = Field(default=3, description="舒畅持续轮数")


class InjectSection(SectionBase):
    """提示词注入。"""

    enabled: bool = Field(default=True, description="是否注入提示词")
    normal_inject_min_tier: int = Field(
        default=2,
        description="普通（非私密）模式下，数值到第几档才注入一句日常感受。3 表示无感与有感都不打扰",
    )
    private_inject: bool = Field(
        default=True,
        description="私密模式（检测到 sisp in_sex_mode）时是否注入完整身体感受",
    )
    disrupt_enabled: bool = Field(
        default=False,
        description=(
            "是否启用羞耻高时的聊天干扰（强制短句 / 带音效 / 不主动展开）。\n"
            "默认关：只陈述感受，不改回复风格"
        ),
    )
    disrupt_from_band: float = Field(
        default=60.0,
        description="羞耻值达到多少才启用干扰（高档起点）",
    )
    short_sentence_limit: int = Field(
        default=15,
        description="干扰档下的建议单句字数上限（写进提示词，不是硬截断）",
    )
    refresh_on_prompt: bool = Field(
        default=True,
        description="是否在 on_prompt_build 时再刷新一次 reminder（防止 on_chatter_step 没跑到）",
    )


class SispBridgeSection(SectionBase):
    """与 sisp 的联动：只读它的状态，判断是否处于私密模式。"""

    enabled: bool = Field(
        default=True,
        description="是否检测 sisp 的私密模式（关闭后永远不会进入私密注入）",
    )
    store_order: list[str] = Field(
        default_factory=lambda: ["deep_neo_sisp", "sisp"],
        description=(
            "按顺序探测的 storage store 名。\n"
            "deep_neo_sisp = 深度改造版；sisp = 市场版与本地版（同 store 名）。\n"
            "探测到第一个存在 in_sex_mode 字段的即采用。"
        ),
    )
    extra_store: str = Field(
        default="",
        description="自定义 store 名（填了就优先试它），用于以后 sisp 再改版时兜底",
    )
    state_key_suffix: str = Field(
        default="",
        description="状态 key 的后缀（默认直接用 stream_id；若某版 sisp 改了 key 规则可在此追加）",
    )
    cache_seconds: float = Field(
        default=3.0,
        description="私密模式的读取缓存秒数（避免同一轮里反复读盘）",
    )


class InitSection(SectionBase):
    """首次加载时按人设推断一次参数。"""

    enabled: bool = Field(
        default=True,
        description="是否允许插件在首次加载时调一次模型，按人设推断易羞 / 肠胃底子 / 屁意倍率",
    )
    task: str = Field(
        default="actor",
        description="推断用哪个模型任务（建议指向便宜的小模型）",
    )
    max_tokens: int = Field(default=200, description="推断请求的最大输出 token")
    temperature: float = Field(default=0.3, description="推断请求的温度")
    context_messages: int = Field(
        default=10,
        description="推断时附带最近多少条对话作为上下文（0 = 只看人设）",
    )


class SceneSection(SectionBase):
    """事件卡（场景卡）：和 sisp 同构的一套。

    卡的效果直接乘在两条数值上，所以"泻药""吃撑了""受凉"这类事
    会真实地改变她憋住的难度，而不只是提示词里的一句话。
    """

    enabled: bool = Field(
        default=True,
        description="是否启用事件卡系统",
    )
    trigger_enabled: bool = Field(
        default=True,
        description="是否用自己的触发词激活卡片（聊天里出现「泻药」「吃撑了」就生效）",
    )
    follow_sisp: bool = Field(
        default=True,
        description=(
            "是否跟随 sisp 的场景卡：sisp 当前生效的场景标签如果和我们的卡同名"
            "（比如 laxative），就直接把我们的同名卡点亮——两边描述同一件事"
        ),
    )
    sync_interval_seconds: int = Field(
        default=30,
        description="跟随 sisp 时，多久同步一次它的场景标签（避免每轮都读盘）",
    )
    disabled_cards: list[str] = Field(
        default_factory=list,
        description="要关掉的卡 id（内置卡：laxative / stomach_ache / gas_hold / cold / banquet）",
    )
    custom_cards: list[dict] = Field(
        default_factory=list,
        description=(
            "自定义或覆盖卡片。每项是一个表：\n"
            "id / title / triggers（列表）/ hours / gas_mult / stool_mult / cap_mult / place / prompt\n"
            "id 与内置卡相同时会覆盖那一条"
        ),
    )
    manual_command: bool = Field(
        default=True,
        description="是否允许 /肠胃 卡 <id> 手动开关卡",
    )


class IntegrationSection(SectionBase):
    """对外联动（全部软依赖：对方不在就静默跳过）。"""

    daily_schedule: bool = Field(
        default=True,
        description=(
            "是否向日程系统（daily_schedule）注入事件：\n"
            "如厕回来 → 「去了一下洗手间（约 N 分钟）」；失禁 → 「肚子不舒服，跑了一趟厕所（约 10 分钟）」。\n"
            "对方没装、或写失败都会静默跳过，不影响插件本身"
        ),
    )


class GutSimConfig(BaseConfig):
    """肠胃模拟插件配置模型。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "肠胃模拟（屁意 / 便意 / 羞耻 / 状态事件）配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(PluginSection):
        """插件基础配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)

    numeric: NumericSection = Field(default_factory=NumericSection)
    grow: GrowSection = Field(default_factory=GrowSection)
    shame: ShameSection = Field(default_factory=ShameSection)
    toilet: ToiletSection = Field(default_factory=ToiletSection)
    scene: SceneSection = Field(default_factory=SceneSection)
    events: EventsSection = Field(default_factory=EventsSection)
    integrations: IntegrationSection = Field(default_factory=IntegrationSection)
    inject: InjectSection = Field(default_factory=InjectSection)
    sisp: SispBridgeSection = Field(default_factory=SispBridgeSection)
    init: InitSection = Field(default_factory=InitSection)

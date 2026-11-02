from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PREFERENCES = {
    "verbosity": ("concise", "standard", "detailed"),
    "language": ("auto", "zh", "en"),
    "terminology": ("plain", "game_terms"),
    "guidance": ("auto", "guided", "practice", "independent"),
}
HABITS = {"play_style": ("solo", "team", "mixed"), "risk": ("cautious", "balanced", "aggressive")}
LABELS = {"verbosity": "表达详略", "language": "回答语言", "terminology": "术语偏好", "guidance": "讲解方式",
          "play_style": "常用玩法", "risk": "自报风险偏好", "schedule": "常见游戏时间", "routine": "日常习惯"}
VALUES = {"concise": "简短", "standard": "适中", "detailed": "详细", "auto": "自动", "zh": "中文", "en": "英文",
          "plain": "易懂称呼", "game_terms": "游戏术语", "guided": "逐步解释", "practice": "适度提示",
          "independent": "直接讨论条件和取舍", "solo": "通常单人", "team": "通常组队", "mixed": "单人和组队都有",
          "cautious": "稳健", "balanced": "均衡", "aggressive": "进取"}
CONCEPTS = {
    "inventory_scope": ("库存范围", r"库存|背包|盘点|缺口|inventory", "未知项不等于零；先区分完整盘点和局部观测。"),
    "craft_batches": ("合成批次", r"批次|余量|剩余材料|多做|batch", "先扣除可用于目标的已有成品，以非负的待制作数量除以每批产出并向上取整。余量是实际产出减待制作数量，不能把目标总量当成净制作需求；预留和共享材料由工具统一核算。"),
    "intermediate_credit": ("中间材料抵扣", r"抵扣|中间材料|已有成品|火药|炸药|craft", "结合工具报告区分已有成品、中间材料和仍需获取的原料。"),
    "workbench_blueprints": ("工作台与蓝图", r"科技|工作台|蓝图|研究|workbench|blueprint", "分别核对工具报告里的工作台条件和已知配方条件，未知条件不能当作已满足。"),
    "raid_conditions": ("拆除条件", r"拆墙|拆门|石墙|爆炸物|几颗|raid", "先明确目标和适用条件，再解释计算报告；最省资源须明确比较指标和方案范围。"),
    "base_entry": ("基地入口检查", r"入口|气闸|门锁|基地布局|被偷家|被抄家", "区分画面可见事实、玩家自报和未观察区域；机制解释需要本轮证据。"),
    "upkeep": ("维护与离线准备", r"维护|衰减|下线|upkeep", "先核对当前维护与离线计划，旧观测不等于当前剩余时间；机制需要本轮证据。"),
    "priorities": ("目标与取舍", r"优先|加固|先.+还是|阶段|发展|取舍", "把目标、当前约束和完成条件联系起来；不能把互斥候选当作同时可行。"),
    "source_version": ("版本与依据", r"版本|依据|来源|更新|patch", "抓取时间与适用版本分开判断，旧历史不能替代当前规则。"),
    "monuments": ("纪念碑路线", r"纪念碑|刷卡|卡房|遗迹|monument", "先核对行动前提，再依据本轮资料解释路线；不靠历史猜测当前机制。"),
}


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "habit", "event", "learning"]
    key: str = Field(max_length=40)
    value: str = Field(min_length=1, max_length=240)
    quote: str = Field(min_length=2, max_length=500)
    signal: Literal["explicit", "needs_help", "self_reported", "demonstrated", "discussed"] = "explicit"

    @model_validator(mode="after")
    def supported(self):
        if self.kind == "preference" and self.value not in PREFERENCES.get(self.key, ()):
            raise ValueError("Unsupported preference")
        if self.kind == "habit" and self.key not in {"schedule", "routine"} and self.value not in HABITS.get(self.key, ()):
            raise ValueError("Unsupported habit")
        if self.kind == "event" and self.key not in {"raided", "outcome", "goal"}:
            raise ValueError("Unsupported event")
        if self.kind == "learning":
            if self.key not in CONCEPTS or self.signal == "explicit":
                raise ValueError("Unsupported learning evidence")
        elif self.signal != "explicit":
            raise ValueError("Only learning has progress signals")
        return self


class MemoryExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=5)


class MemorySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class MemoryPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str

    @model_validator(mode="after")
    def supported(self):
        if self.value not in PREFERENCES.get(self.key, ()):
            raise ValueError("Unsupported preference")
        return self

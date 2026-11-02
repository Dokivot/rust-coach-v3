from html import escape
from pathlib import Path


WIDTH = 2200
HEIGHT = 2200
COLORS = {
    "D": ("#e8f3ff", "#2864a0", "#12324f"),
    "G": ("#f1eaff", "#8652b3", "#45276b"),
    "S": ("#e8f5ef", "#368265", "#174c39"),
    "N": ("#f7f8fb", "#718096", "#26354a"),
    "R": ("#fff5df", "#ad7b20", "#684710"),
}
parts = []


def text_line(horizontal, vertical, content, size=22, weight=400, color="#26354a"):
    parts.append(
        f'<text x="{horizontal}" y="{vertical}" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}">{escape(content)}</text>'
    )


def panel(horizontal, vertical, width, height, title):
    parts.append(
        f'<rect x="{horizontal}" y="{vertical}" width="{width}" '
        f'height="{height}" rx="20" fill="#ffffff" stroke="#d8e0ec" stroke-width="2"/>'
    )
    text_line(horizontal + 24, vertical + 38, title, 25, 700)


def box(horizontal, vertical, width, height, kind, title, lines):
    fill, stroke, foreground = COLORS[kind]
    parts.append(
        f'<rect x="{horizontal}" y="{vertical}" width="{width}" height="{height}" '
        f'rx="12" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    prefix = "D · " if kind == "R" else f"{kind} · " if kind != "N" else ""
    text_line(horizontal + 18, vertical + 32, prefix + title, 23, 700, foreground)
    for index, line in enumerate(lines):
        text_line(horizontal + 18, vertical + 62 + index * 28, line, 20, 400, foreground)


def arrow(path, label=None, label_x=0, label_y=0, dashed=False, color="#4a637e"):
    dash = ' stroke-dasharray="8 7"' if dashed else ""
    marker = "arrow-green" if color == "#368265" else "arrow"
    parts.append(
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" '
        f'stroke-linejoin="round"{dash} marker-end="url(#{marker})"/>'
    )
    if label:
        parts.append(
            f'<text x="{label_x}" y="{label_y}" font-size="18" fill="{color}" '
            f'paint-order="stroke" stroke="#ffffff" stroke-width="7" '
            f'stroke-linejoin="round">{escape(label)}</text>'
        )


def render():
    parts.clear()
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title description">'
    )
    parts.append('<title id="title">Rust Coach v2 · 六层架构与知识层</title>')
    parts.append(
        '<desc id="description">包含输入、理解、结构化知识、向量检索、规划中的知识图谱、'
        '工作/情景/语义记忆、Plan-and-Execute、Agentic RAG、生成、持久化、知识发布与评测。'
        '蓝色为确定性组件，紫色为生成式组件，绿色为存储，金色为预留计算接口。'
        'RAG 包括向量语义检索和互联网实时搜索；两个循环支持中途持久澄清。</desc>'
    )
    parts.append(
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#4a637e"/></marker>'
        '<marker id="arrow-green" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#368265"/></marker></defs>'
    )
    parts.append(
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#f3f6fb"/>'
        '<g transform="translate(80 0)" '
        'font-family="PingFang SC, Noto Sans CJK SC, Microsoft YaHei, sans-serif">'
    )
    text_line(48, 68, "Rust Coach v2 · LangGraph 技术架构", 42, 800, "#142c47")
    text_line(48, 109, "Plan-and-Execute 决策  +  Agentic RAG  +  中途澄清与恢复  |  两类计算只预留接口", 24)
    for horizontal, kind, caption in (
        (48, "D", "确定性：代码 / 工具 / 规则"),
        (500, "G", "生成式：LLM / 视觉模型"),
        (940, "S", "持久化存储"),
    ):
        fill, stroke, _ = COLORS[kind]
        parts.append(
            f'<rect x="{horizontal}" y="138" width="22" height="22" '
            f'rx="5" fill="{fill}" stroke="{stroke}"/>'
        )
        text_line(horizontal + 34, 157, f"{kind}  {caption}", 20)
    text_line(1190, 157, "金色：预留接口   实线：在线流   虚线：保存 / 发布 / 观测", 20)

    panel(40, 190, 1960, 175, "01  输入层：UserInput 多模态处理与 Agent Runtime")
    box(70, 245, 390, 95, "D", "输入 API", ["提示词 / 图片 · 鉴权 / 幂等"])
    box(540, 245, 730, 95, "D", "InputPipeline + Runtime", ["文本抽取 / 视觉抽取 / 跨模态融合 / 校验", "预算 / 超时 / 取消 / SSE · 恢复同一 task"])
    box(1400, 245, 540, 95, "N", "玩家", ["提问 / 上报状态 / 回答澄清 / 查看结果"])
    arrow("M1400 275 H1380 V235 H265 V245", "提示词、附件引用、补充回答", 700, 230)
    arrow("M460 293 H540", "UserInput", 474, 281)

    panel(40, 410, 1280, 290, "02  理解层 · CoachTaskRepresentation 与条件路由")
    box(70, 465, 280, 100, "D", "ParsedInput 校验", ["请求/状态候选、证据", "unknowns / 版本 / 引用"])
    box(390, 465, 280, 100, "G", "意图识别", ["目标、子目标、实体候选", "硬/软约束与期望结果"])
    box(710, 465, 280, 100, "D", "状态解析 / 合并", ["scope / revision / TTL", "来源、覆盖、新鲜度、冲突"])
    box(1030, 465, 240, 100, "D", "任务表示", ["CoachTaskRepresentation", "证据 + 不确定性"])
    box(390, 610, 280, 70, "D", "知识查询 / 重理解", ["KnowledgeQuery → 新 revision"])
    box(710, 610, 280, 70, "D", "路由 / 澄清恢复", ["知识、计算、决策或 input"])
    arrow("M680 340 V388 H210 V465", "ParsedInput", 230, 391)
    arrow("M1100 340 V390 H850 V465", "运行时上下文", 900, 385)
    arrow("M350 515 H390", "候选", 355, 503)
    arrow("M670 515 H710", "实体与约束", 672, 503)
    arrow("M990 515 H1030", "任务表示", 993, 503)
    arrow("M1150 565 V592 H530 V610", "知识缺口", 1010, 586)
    arrow("M670 645 H710", "已补充证据", 674, 633)
    arrow("M850 565 V592 H1030 V565", "完成理解", 895, 586)
    arrow("M850 680 V695 H1150 V565", "澄清/重理解", 930, 692)

    panel(1360, 410, 640, 700, "知识层 · Agentic RAG 子图")
    box(1400, 470, 540, 80, "G", "查询规划 / 缺口改写", ["围绕 EvidenceNeed，改写未覆盖需求"])
    box(1400, 580, 540, 70, "D", "来源选择 / 时效策略", ["vector / web / both · 全局预算守卫"])
    box(1400, 680, 250, 100, "D", "语义检索", ["pgvector + metadata", "关键词融合 / 重排"])
    box(1690, 680, 250, 100, "D", "实时搜索", ["web search → fetch", "正文 / 时间 / hash"])
    box(1400, 830, 250, 90, "G", "证据评估", ["相关 / 支持 / 反驳"])
    box(1690, 830, 250, 90, "D", "覆盖门禁", ["版本 / 冲突 / 预算"])
    box(1400, 980, 540, 95, "D", "EvidenceBundle", ["声明-证据矩阵、缺口、冲突与来源快照", "已充分则返回；缺玩家状态可原地中断"])
    arrow("M1670 550 V580")
    arrow("M1590 650 V665 H1525 V680", "本地证据", 1412, 674)
    arrow("M1750 650 V665 H1815 V680", "最新请求主动联网", 1715, 674)
    arrow("M1525 780 V830", "归一 / 去重", 1415, 814)
    arrow("M1815 780 V805 H1640 V830", "正文", 1770, 801)
    arrow("M1650 875 H1690")
    arrow("M1815 920 V950 H1670 V980", "充分 / 明确降级", 1730, 945)
    arrow("M1940 875 H1960 V510 H1940")
    parts.append('<text transform="translate(1985 850) rotate(-90)" font-size="18" fill="#4a637e">缺资料 → 有界补检索</text>')

    panel(40, 740, 1280, 370, "03  决策层 · SimpleCall + Plan-and-Execute")
    box(70, 800, 350, 90, "D", "SimpleCallChain", ["普通聊天直达生成层", "强计算：一次 CalculatorPort"])
    box(500, 800, 340, 90, "G", "Planner / Replanner", ["任务执行计划 · 保留有效成果"])
    box(920, 800, 350, 90, "D", "计划校验 / Executor", ["ready step · 动态工具 / 子图"])
    box(500, 960, 340, 105, "D", "行动候选评估", ["依赖、前置条件、资源与取舍", "未知预算 → conditional"])
    box(920, 960, 350, 105, "D", "步骤验证 / 进度判定", ["继续 / 重规划 / 澄清 / 结束", "必要步骤完成 → ActionPlan"])
    box(70, 960, 350, 105, "R", "CalculatorPort 预留", ["拆建筑爆炸物 / 合成路径缺口", "暂不实现；未配置不输出数字"])
    arrow("M970 580 V605 H890 V715 H245 V800", "事实 / 强计算", 267, 720)
    arrow("M1035 580 V598 H865 V725 H670 V800", "多目标 / 多步", 681, 770)
    arrow("M840 845 H920", "计划", 856, 830)
    arrow("M1095 890 V930 H670 V960", "候选行动", 1000, 925)
    arrow("M245 890 V960", "计算接口调用", 265, 937)
    arrow("M920 845 H885 V915 H350 V960", "可选计算步骤", 480, 908)
    arrow("M840 1010 H920", "结果", 854, 998)
    arrow("M420 1010 H450 V1088 H1095 V1065", "计算结果 / unavailable", 506, 1081)
    arrow("M1000 960 V945 H470 V898 H670 V890", "重规划", 480, 938)
    arrow("M1200 960 V890", "下一步", 1211, 928)
    arrow("M1270 820 H1325 V525 H1400", "证据需求", 1280, 510)
    arrow("M1400 1018 H1270", "证据包", 1295, 1006)
    arrow("M1270 990 H1290 V655 H1270", "中途缺信息", 1277, 730)
    arrow("M1400 1045 H1310 V675 H1270")

    panel(40, 1160, 1280, 400, "04  生成层 · 数值与行动先验证，风格后组织")
    box(70, 1220, 350, 110, "D", "AnswerSpec", ["已验证 claim / action ID", "计算步骤、假设、来源与限制"])
    box(500, 1220, 340, 110, "G", "可选表达组织", ["稳定人设 · 解释取舍", "引用声明；不重算关键数字"])
    box(920, 1220, 350, 110, "D", "渲染 / 输出门禁", ["模板注入数值、单位与对象", "引用校验 / 隐私 / 注入防护"])
    box(70, 1420, 1200, 100, "N", "交付给玩家的答案", ["结论 → 可执行步骤 → 资源差额与取舍 → 适用条件 → 可展开的计算与来源"])
    arrow("M1180 1065 V1130 H245 V1220", "已验证行动 / 条件性结果 / 明确限制", 320, 1149)
    arrow("M420 1270 H500", "可选", 436, 1258)
    arrow("M840 1270 H920", "框架", 855, 1258)
    arrow("M245 1330 V1375 H1095 V1330", "默认：确定性模板", 513, 1365)
    arrow("M1200 1330 V1420", "放行 / 安全模板回退", 991, 1405)

    panel(1360, 1160, 640, 580, "Knowledge Stores / Memory / Persistence")
    box(1400, 1220, 540, 110, "S", "工作记忆 / Checkpointer", ["TaskState、EvidenceNeed、候选与假设", "任务级中断 / 崩溃恢复；不跨任务当事实"])
    box(1400, 1380, 540, 135, "S", "玩家状态 / 会话 / 个人记忆", ["状态：范围 / revision / wipe / 有效期", "情景记忆：事件 / 结果 / scope / TTL", "语义记忆：偏好 / 习惯 / 学习进度 / 遗忘"])
    box(1400, 1570, 540, 110, "S", "知识存储与证据", ["结构化库：规则 / 行动 / capability", "pgvector：chunks；知识图谱〔规划〕", "网页快照 / claims / 版本与发布指针"])
    arrow("M1270 300 H1338 V1138 H1920 V1220", "保存 checkpoint", 1400, 1130, True, "#368265")
    arrow("M1600 1330 V1350 H1350 V390 H1240 V340", "同任务恢复", 1400, 1344, True, "#368265")
    arrow("M1400 1455 H1300 V590 H805 V580", "读取状态 / 历史 / 偏好", 680, 608, True, "#368265")
    arrow("M1270 1470 H1380 V1615 H1400", "归档 + outbox", 1196, 1606, True, "#368265")
    arrow("M1670 1570 V1515", "知识 / 记忆更新", 1690, 1546, True, "#368265")

    panel(40, 1590, 1280, 150, "知识构建 · 实时结果不自动污染正式语料")
    box(70, 1645, 1200, 75, "D", "来源审核 → 条件化分块 → embedding → 回归 → 不可变 kb_release", ["新网页先保存任务快照，后经 staging 审核入库；计算规则不从网页临时生成执行"])
    arrow("M1270 1690 H1400", "发布索引", 1290, 1715, True, "#368265")
    arrow("M1670 1570 V1540 H1370 V730 H1400", "读取索引 / 语料", 1410, 1559, True, "#368265")
    arrow("M1940 730 H2010 V1620 H1940", "保存实时正文快照", 1730, 1770, True, "#368265")

    panel(40, 1790, 1960, 290, "05  评测与观测 · 跨层埋点与质量闭环")
    box(70, 1860, 570, 125, "D", "Trace / Metrics / 成本", ["节点、模型、工具、版本、输入 hash、校验码", "延迟、token、费用、终止原因；默认脱敏"])
    box(730, 1860, 570, 125, "D", "Badcase 分类与人工审核", ["用户反馈 / 校验失败 / 抽样审查", "区分计划、检索、状态、证据、表达与恢复"])
    box(1390, 1860, 550, 125, "D", "独立评测与发布门禁", ["行动可行 / 检索覆盖 / 实时性 / 嵌套恢复", "假数字拦截 / 隔离 / 延迟 / 成本 / 回归"])
    arrow("M90 1520 H55 V1760 H355 V1860", "全节点 / 工具 / 模型 span", 377, 1770, True)
    arrow("M640 1920 H730", "案例", 657, 1905, True)
    arrow("M1300 1920 H1390", "金标", 1318, 1905, True)
    arrow("M1665 1860 V1750 H1000 V1720", "评测发布门禁", 1019, 1769, True, "#368265")
    text_line(70, 2033, "终止条件：成功 / 部分结果 / 缺证据 / 预算耗尽 / 取消；等待补充信息是持久化暂停。", 22, 500)
    text_line(48, 2130, "可靠性边界：计划必须执行验证；证据缺口可补检索，玩家信息缺口必须询问；未接入计算工具不造数字。", 24, 700)
    text_line(48, 2166, "当前为设计交付：主图 + 两个子图 + PostgreSQL/pgvector + 实时搜索接口 + 两个预留 CalculatorPort。", 21)
    parts.append("</g></svg>")
    destination = Path(__file__).with_name("architecture.svg")
    destination.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    render()

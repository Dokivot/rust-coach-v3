from pathlib import Path
import html
import json


ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 3000, 2440
STYLE = {
    "done": ("#258453", "", "已实现"),
    "planned": ("#c77a18", "11 6", "已设计未实现"),
    "tbd": ("#79818b", "2 6", "待设计"),
}
nodes, edges, layers, parts = [], [], [], []


def text(x, y, value, size=25, weight=400, fill="#3c5065", anchor="start", extra=""):
    parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" {extra}>{html.escape(value)}</text>')


def rect(x, y, width, height, fill="#f8fafc", stroke="#ccd6e0", extra=""):
    parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{fill}" stroke="{stroke}" {extra}/>')


def node(identifier, title, duty, x, y, kind="D", status="done", width=500):
    stroke, dash, label = STYLE[status]
    item = dict(id=identifier, title=title, duty=duty, x=x, y=y, width=width, height=120, kind=kind, status=status)
    nodes.append(item)
    parts.append(f'<g id="{identifier}" class="component" data-kind="{kind}" data-status="{status}">')
    rect(x, y, width, 120, "#edf5fc" if kind == "D" else "#f2edfa", stroke, f'stroke-width="2.5" stroke-dasharray="{dash}"')
    text(x + 18, y + 25, kind, 20, 700)
    text(x + width - 18, y + 25, label, 19, 500, stroke, "end")
    text(x + 18, y + 60, title, 28, 700, "#18324b")
    text(x + 18, y + 98, duty, 23)
    parts.append('</g>')


def edge(identifier, source, target, points, label, position, kind="data"):
    dash = 'stroke-dasharray="10 7"' if kind == "control" else ''
    parts.append(f'<g class="connection" data-relation="{identifier}">')
    parts.append(f'<polyline points="{" ".join(f"{x},{y}" for x, y in points)}" fill="none" stroke="#526a80" stroke-width="2.5" {dash} marker-end="url(#arrow)"/>')
    text(*position, label, 22, 500, "#415970", "middle", 'class="edge-label"')
    parts.append('</g>')
    edges.append(dict(id=identifier, source=source, target=target, points=points, payload=label, kind=kind))


def main():
    text(60, 65, "Rust Coach · 核心技术架构", 44, 700, "#18324b")
    text(60, 108, "模型负责理解、规划与表达；确定性服务负责精确计算、状态校验与执行。", 27)
    text(2940, 65, "当前实现 + 目标扩展", 25, 600, anchor="end")
    text(2940, 106, "单体应用 · 六层职责 · 2026-11-03", 22, anchor="end")
    rect(40, 135, 2920, 90)
    rect(65, 156, 220, 48, "#edf5fc")
    text(175, 188, "D 确定性组件", 23, 600, anchor="middle")
    rect(305, 156, 220, 48, "#f2edfa")
    text(415, 188, "G 生成式组件", 23, 600, anchor="middle")
    for offset, status in enumerate(STYLE):
        stroke, dash, label = STYLE[status]
        rect(570 + offset * 280, 156, 250, 48, "#fff", stroke, f'stroke-width="2.5" stroke-dasharray="{dash}"')
        text(695 + offset * 280, 188, label, 23, anchor="middle")
    edge("legend-data", "", "", [(1460, 182), (1550, 182)], "数据流", (1640, 188))
    edge("legend-control", "", "", [(1770, 182), (1860, 182)], "调用 / 控制", (1990, 188), "control")
    text(2180, 188, "填色、边框、连线分别编码。", 23)
    text(60, 290, "共享状态与存储", 26, 700)
    text(2490, 290, "外部服务与目标能力", 26, 700)
    for index, (title, duty) in enumerate([
        ("输入层", "接收用户请求与玩家信息"),
        ("理解层", "识别目标并选择执行路径"),
        ("知识层", "提供工具结果与可追溯证据"),
        ("决策层", "按任务调用工具，必要时澄清"),
        ("生成层", "将任务结果转为可信的教练回答"),
        ("评测与观测层", "横向观察全链路并持续改进质量"),
    ], 1):
        y = 350 + (index - 1) * 290
        layers.append(dict(index=index, title=title, x=550, y=y, width=1840, height=245))
        rect(550, y, 1840, 245, "#f8fafc", "#bdcbd8")
        text(580, y + 37, f'{index:02}  {title}', 29, 700, "#18324b")
        text(1050, y + 37, duty, 24)
    node("input", "请求与附件接入", "校验文字、截图与状态输入", 600, 440)
    node("vision", "多模态识别", "提取截图中的物品与可见状态", 1210, 440, "G")
    node("context", "玩家状态与上下文", "按范围、时效组织状态与记忆", 1820, 440)
    node("route", "任务识别与路由", "规则分流计算、建议、聊天与观察", 600, 730)
    node("params", "语义参数提取", "将复杂计算请求转为参数候选", 1210, 730, "G")
    node("understand", "通用语义理解", "处理多意图、实体消歧与上下文", 1820, 730, "G", "planned")
    node("calculator", "确定性计算引擎", "按规则计算配方、库存与拆除成本", 600, 1020)
    node("evidence", "知识检索与证据", "查询游戏规则并获取可追溯正文", 1210, 1020)
    node("rag", "知识推理与补充检索", "评估证据缺口并规划有界补查", 1820, 1020, "G", "planned")
    node("executor", "工具执行与校验", "校验参数、调用工具并整合结果", 600, 1310)
    node("planner", "任务规划", "生成受限候选方案与行动计划", 1210, 1310, "G")
    node("task", "持久任务协调", "管理步骤、预算、澄清与恢复", 1820, 1310, "D", "planned")
    node("results", "结果整合与保护", "锁定数值、条件与引用并安全回退", 600, 1600)
    node("coach", "Coach 回答生成", "基于任务结果组织个性化表达", 1210, 1600, "G")
    node("claims", "统一事实门禁", "生成前约束声明，输出后核查保真", 1820, 1600, "D", "planned")
    node("localobs", "日志与回归评测", "记录局部事件并验证已有行为", 600, 1890)
    node("telemetry", "全链路观测", "追踪任务、调用、延迟与成本", 1210, 1890, "D", "planned")
    node("quality", "质量评测与发布", "用任务成功率与失败案例驱动改进", 1820, 1890, "D", "planned")
    node("localstore", "本地会话与状态库", "保存状态、记忆、附件与成功对话", 60, 440, width=420)
    node("rules", "游戏规则与证据库", "供计算与检索共享规则和正文快照", 60, 1020, width=420)
    node("checkpoint", "目标任务存储", "持久保存步骤与中间结果", 60, 1310, status="planned", width=420)
    node("model", "模型服务 · LLM / VLM", "供紫色模块共享调用", 2490, 440, "G", width=450)
    node("search", "搜索与公开来源", "提供检索链接与网页正文", 2490, 910, width=450)
    node("knowledge", "目标统一知识库", "结构化数据、向量与关系索引", 2490, 1200, status="planned", width=450)
    rect(600, 260, 500, 60, "#fff", "#aabaca")
    text(850, 300, "玩家请求 · 文字 / 截图 / 状态", 25, 600, anchor="middle")
    rect(600, 2110, 1110, 80, "#fff", "#aabaca")
    text(1155, 2160, "最终回答 · 建议 / 计算结果 / 依据 / 澄清", 28, 600, anchor="middle")
    edge("request", "", "input", [(850, 320), (850, 436)], "用户请求", (970, 412))
    edge("visual", "vision", "input", [(1210, 500), (1105, 500)], "观察", (1155, 481))
    edge("parsed", "input", "route", [(850, 560), (850, 726)], "归一化输入", (720, 630))
    edge("parameters", "params", "route", [(1210, 790), (1105, 790)], "参数", (1155, 771))
    edge("route-task", "route", "executor", [(600, 820), (520, 820), (520, 1370), (595, 1370)], "任务 / 参数", (390, 1200))
    edge("call-calc", "executor", "calculator", [(600, 1330), (570, 1330), (570, 1110), (595, 1110)], "调用", (595, 1165), "control")
    edge("calculation", "calculator", "executor", [(850, 1140), (850, 1306)], "计算结果", (960, 1200))
    edge("plan", "planner", "executor", [(1210, 1370), (1105, 1370)], "计划", (1155, 1351))
    edge("retrieve", "planner", "evidence", [(1680, 1310), (1680, 1144)], "取证", (1740, 1260), "control")
    edge("evidence-result", "evidence", "planner", [(1460, 1140), (1460, 1306)], "检索结果 / 证据集合", (1300, 1200))
    edge("task-result", "executor", "results", [(850, 1430), (850, 1596)], "任务结果", (980, 1490))
    edge("draft", "coach", "results", [(1210, 1660), (1105, 1660)], "措辞", (1155, 1641))
    edge("answer", "results", "", [(850, 1720), (850, 1765), (520, 1765), (520, 2150), (595, 2150)], "最终回答", (390, 2080))
    edge("model-call", "vision", "model", [(1460, 440), (1460, 415), (2450, 415), (2450, 500), (2485, 500)], "共享模型调用", (2080, 400), "control")
    edge("store", "localstore", "input", [(480, 500), (595, 500)], "状态", (535, 481))
    edge("context-route", "context", "route", [(2070, 560), (2070, 615), (1000, 615), (1000, 726)], "玩家状态 / 历史 / 记忆", (1660, 602))
    edge("search-data", "search", "evidence", [(2490, 970), (2450, 970), (2450, 900), (1460, 900), (1460, 1016)], "公开正文", (2230, 890))
    edge("knowledge-data", "knowledge", "rag", [(2490, 1260), (2420, 1260), (2420, 1080), (2325, 1080)], "知识 / 证据", (2660, 1160))
    edge("semantic-route", "understand", "route", [(2070, 850), (2070, 875), (1040, 875), (1040, 854)], "目标语义任务", (1600, 867))
    edge("rag-plan", "task", "rag", [(2070, 1310), (2070, 1144)], "缺证：补查", (2200, 1200), "control")
    edge("rag-result", "rag", "task", [(1930, 1140), (1930, 1306)], "证据", (1850, 1200))
    edge("coordination", "task", "planner", [(1820, 1370), (1715, 1370)], "重规划", (1765, 1351), "control")
    edge("claims", "task", "claims", [(2070, 1430), (2070, 1596)], "结果 / 证据 / 条件", (2220, 1490))
    edge("compose-input", "executor", "coach", [(1100, 1410), (1150, 1410), (1150, 1515), (1460, 1515), (1460, 1596)], "任务结果 / 证据", (1440, 1503))
    edge("constrain", "claims", "coach", [(1820, 1660), (1715, 1660)], "约束", (1765, 1641))
    edge("log", "results", "localobs", [(850, 1720), (850, 1886)], "检查与日志", (980, 1830))
    edge("quality", "telemetry", "quality", [(1710, 1950), (1815, 1950)], "回放", (1765, 1931))
    edge("traces", "task", "telemetry", [(2390, 1267), (2460, 1267), (2460, 1855), (1460, 1855), (1460, 1886)], "全链路事件 / 延迟 / 用量", (2060, 1840))
    text(2490, 1585, "共享依赖", 26, 700)
    text(2490, 1625, "模型服务供全部 G 模块复用。", 23)
    text(2490, 1660, "任务协调使用目标任务存储。", 23)
    text(2490, 1695, "本地存储由运行时共享读写。", 23)
    text(2490, 1810, "观测范围", 26, 700)
    text(2490, 1850, "当前：局部事件与软件回归。", 23)
    text(2490, 1885, "目标：横跨输入、理解、检索、", 23)
    text(2490, 1920, "执行与生成的完整调用追踪。", 23)
    text(60, 1615, "当前执行边界", 25, 700)
    text(60, 1655, "有界规划与候选核算已实现。", 23)
    text(60, 1690, "通用重规划、同任务澄清恢复", 23)
    text(60, 1725, "属于橙色目标能力。", 23)
    text(60, 1760, "聊天与观察可直接进入生成层。", 23)
    text(60, 1900, "异常处理", 25, 700)
    text(60, 1940, "缺条件 → 澄清或条件回答。", 23)
    text(60, 1975, "工具失败 → 降级或明确报错。", 23)
    text(60, 2010, "不把失败伪装成成功。", 23)
    text(60, 2290, "核心主链路：输入 → 理解 → 执行 → 回答；知识服务按需调用，评测与观测横跨全链路。", 28, 600)
    text(60, 2340, "算法、接口与完整分支见技术设计文档。灰色图例保留，未定型领域能力不占核心节点。", 24)
    defs = '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#526a80"/></marker></defs>'
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img"><title>Rust Coach 六层核心技术架构总览</title>{defs}<style>text{{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}}.edge-label{{paint-order:stroke;stroke:white;stroke-width:5;stroke-linejoin:round}}</style><rect width="100%" height="100%" fill="white"/>{"".join(parts)}</svg>'
    (ROOT / "technical-architecture-target.svg").write_text(svg, encoding="utf-8")
    manifest = dict(width=WIDTH, height=HEIGHT, layers=layers, nodes={item["id"]: item for item in nodes}, relations=[item for item in edges if not item["id"].startswith("legend")], geometry=dict(nodes=nodes, lines=edges))
    (ROOT / "diagrams" / "architecture-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f'{len(nodes)} nodes; {len(edges) - 2} core relations; {WIDTH} × {HEIGHT}')


if __name__ == "__main__":
    main()

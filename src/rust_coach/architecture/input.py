from __future__ import annotations

import re
from uuid import UUID, uuid4

from rust_coach.architecture.contracts import (
    AttachmentRef,
    ParsedInput,
    Provenance,
    RequestCandidate,
    StateCandidate,
)


class InputLayer:
    """Convert the existing HTTP/vision inputs into the versioned ParsedInput contract."""

    _calculation_terms = re.compile(
        r"爆炸|炸药|炸墙|炸门|拆|石墙|铁墙|木墙|金属墙|装甲墙|车库门|铁门|金属门|装甲门|合成|制作|缺口|材料|配方|"
        r"还差|造出|造一|做一|几颗|几发|多少发|多少颗|最省|算一下|C4|火箭|硫磺|craft|raid|gunpowder|explosive",
        re.I,
    )
    _advice_terms = re.compile(
        r"/coach|/search|建议|应该|优先|先.+还是|冲科技|加固|布局|权衡|划算|下一步|攻略|怎么办|如何",
        re.I,
    )

    def __init__(self):
        self.text_extractor = TextExtractor()
        self.fusion = CrossModalFusion()

    def parse(self, request, *, task_id: UUID | None = None, visual: dict | None = None) -> ParsedInput:
        task_id = task_id or uuid4()
        prompt = request.message.strip()
        attachments = tuple(AttachmentRef(attachment_id=item) for item in request.attachment_ids)
        candidates = [self.text_extractor.extract(prompt)]
        state_candidates = list(self.fusion.merge(visual))
        unknowns = []
        if attachments and not visual:
            unknowns.append("visual_extraction_pending")
        if visual:
            unknowns.extend(str(item) for item in visual.get("unknowns", []))
            if not visual.get("usable"):
                unknowns.append("visual_observation_not_usable")
        provenance = [Provenance(source_ref="text:0", source_type="text")] if prompt else []
        provenance.extend(Provenance(source_ref=f"attachment:{item}", source_type="attachment") for item in request.attachment_ids)
        return ParsedInput(
            request_id=request.request_id,
            task_id=task_id,
            prompt=prompt,
            attachments=attachments,
            request_candidates=tuple(candidates),
            player_state_candidates=tuple(state_candidates),
            unknowns=tuple(dict.fromkeys(unknowns)),
            provenance=tuple(provenance),
        )

class TextExtractor:
    _calculation_terms = InputLayer._calculation_terms
    _advice_terms = InputLayer._advice_terms

    def extract(self, prompt: str) -> RequestCandidate:
        return RequestCandidate(
            kind=self.request_kind(prompt),
            operation=self.operation(prompt),
            target=self.target(prompt),
            desired_outcome=prompt or "识别图片中的可见内容",
            text=prompt or "图片输入",
            source_ref="text:0" if prompt else "attachment:0",
        )

    def request_kind(self, prompt: str) -> str:
        if prompt.strip() == "/state":
            return "state.query"
        if self._calculation_terms.search(prompt):
            return "calculation.request"
        if self._advice_terms.search(prompt):
            return "decision.recommendation"
        return "conversation.general"

    @staticmethod
    def operation(prompt: str) -> str | None:
        if re.search(r"合成|制作|配方|缺口|C4|火箭|gunpowder|craft", prompt, re.I):
            return "craft"
        if re.search(r"爆炸|炸|拆|raid|墙|门", prompt, re.I):
            return "raid"
        return None

    @staticmethod
    def target(prompt: str) -> str | None:
        if re.search(r"C4|定时炸弹", prompt, re.I):
            return "explosive.timed"
        return None

class CrossModalFusion:
    @staticmethod
    def merge(visual: dict | None) -> list[StateCandidate]:
        result = []
        for observation in (visual or {}).get("observations", []):
            item_id = observation.get("item_id")
            if not item_id:
                continue
            result.append(StateCandidate(
                field=f"visual.{observation.get('region_kind', 'unknown')}.{item_id}",
                value=observation.get("quantity"),
                status="candidate",
                coverage="partial",
                source=Provenance(source_ref=f"visual:{observation.get('id', 'unknown')}", source_type="visual"),
            ))
        return result


class ParsedInputBuilder:
    def __init__(self, input_layer: InputLayer | None = None):
        self.input_layer = input_layer or InputLayer()

    def build(self, request, *, task_id: UUID | None = None, visual: dict | None = None) -> ParsedInput:
        return self.input_layer.parse(request, task_id=task_id, visual=visual)


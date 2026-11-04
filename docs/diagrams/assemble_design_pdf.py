from pathlib import Path
import json

from pypdf import PdfReader, PdfWriter, Transformation


ROOT = Path(__file__).resolve().parents[2]
TEMP = ROOT / 'tmp' / 'pdfs'
OUTPUT = ROOT / 'output' / 'pdf'
OUTPUT.mkdir(parents=True, exist_ok=True)
reader = PdfReader(TEMP / 'design-body.pdf')
figure = PdfReader(TEMP / 'architecture-vector.pdf')
assert len(figure.pages) == 1
writer = PdfWriter()
writer.clone_document_from_reader(reader)
page = writer.pages[-1]
figure_page = figure.pages[0]
margin = 45
available_width = float(page.mediabox.width) - 2 * margin
available_height = float(page.mediabox.height) - 155
scale = min(available_width / float(figure_page.mediabox.width), available_height / float(figure_page.mediabox.height))
left = (float(page.mediabox.width) - float(figure_page.mediabox.width) * scale) / 2
page.merge_transformed_page(figure_page, Transformation().scale(scale).translate(left, 65))
writer.add_metadata({'/Title': 'Rust Coach 技术设计文档 - 独立阅读版', '/Author': 'Rust Coach 项目', '/Subject': '六层技术架构、当前实现、目标设计与开发路线', '/Keywords': 'Rust Coach, Agent, 技术设计, 六层架构'})
output = OUTPUT / 'Rust-Coach-技术设计文档.pdf'
with output.open('wb') as stream:
    writer.write(stream)
result = PdfReader(output)
text = '\n'.join(page.extract_text() or '' for page in result.pages)
required = ['项目概述与核心能力', '总体技术架构', '六层架构详细设计', 'Agent 核心执行流程', '知识库、数据管理与计算引擎', '玩家状态与上下文管理', '评测与可观测性设计', '当前实现与目标架构差距', '后续开发计划', '事实依据与交付验收', '附录 A', '确定性计算引擎']
missing = [heading for heading in required if heading not in text]
assert not missing, missing
assert not any('/URI' in annotation.get_object().get('/A', {}) for page in result.pages for annotation in page.get('/Annots', []))
report = {'pages': len(result.pages), 'bytes': output.stat().st_size, 'required_sections_present': True, 'external_file_links': 0, 'page_text_lengths': [len(page.extract_text() or '') for page in result.pages]}
(TEMP / 'pdf-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
print(output)

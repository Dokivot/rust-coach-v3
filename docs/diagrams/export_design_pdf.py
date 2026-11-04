from pathlib import Path
import html
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, A2, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak, NextPageTemplate, Table, TableStyle, KeepTogether
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).resolve().parents[2]
TEMP = ROOT / 'tmp' / 'pdfs'
TEMP.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('CJK', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'))
pdfmetrics.registerFontFamily('CJK', normal='CJK', bold='CJK', italic='CJK', boldItalic='CJK')
INK = colors.HexColor('#20364b')
BLUE = colors.HexColor('#315978')
MUTED = colors.HexColor('#62788b')
styles = getSampleStyleSheet()
for style in styles.byName.values():
    style.fontName = 'CJK'
    style.wordWrap = 'CJK'
styles.add(ParagraphStyle('BodyCN', fontName='CJK', fontSize=10.2, leading=16.5, textColor=INK, spaceAfter=8, wordWrap='CJK', splitLongWords=True))
styles.add(ParagraphStyle('H2CN', parent=styles['BodyCN'], fontSize=17, leading=24, textColor=BLUE, spaceBefore=16, spaceAfter=13, keepWithNext=True))
styles.add(ParagraphStyle('H3CN', parent=styles['BodyCN'], fontSize=12.5, leading=19, textColor=BLUE, spaceBefore=12, spaceAfter=8, keepWithNext=True))
styles.add(ParagraphStyle('CellCN', parent=styles['BodyCN'], fontSize=8.6, leading=13.8, spaceAfter=0))
styles.add(ParagraphStyle('SmallCN', parent=styles['BodyCN'], fontSize=8.5, leading=13, textColor=MUTED))
styles.add(ParagraphStyle('CodeCN', parent=styles['BodyCN'], fontSize=8.4, leading=13, backColor=colors.HexColor('#f0f4f7'), borderPadding=8, spaceBefore=7, spaceAfter=12))
styles.add(ParagraphStyle('CoverTitle', parent=styles['BodyCN'], fontSize=32, leading=45, textColor=BLUE))
styles.add(ParagraphStyle('CoverSub', parent=styles['BodyCN'], fontSize=17, leading=28, textColor=MUTED))
styles.add(ParagraphStyle('TOC1', parent=styles['BodyCN'], fontSize=11, leading=19, spaceBefore=8))
styles.add(ParagraphStyle('TOC2', parent=styles['BodyCN'], fontSize=9, leading=14, leftIndent=16, textColor=MUTED))


def inline(value):
    tokens = []
    def replace_code(match):
        tokens.append('<font color="#315978">' + html.escape(match.group(1)) + '</font>')
        return f'CODETOKEN{len(tokens) - 1}END'
    value = re.sub(r'`([^`]+)`', replace_code, value)
    def replace_link(match):
        label, target = match.groups()
        if target.startswith(('https://', 'http://')):
            return label + '（' + target + '）'
        target = target.removeprefix('../')
        if '/' not in target:
            target = 'docs/' + target
        elif target.startswith('diagrams/'):
            target = 'docs/' + target
        return target if label in target else label + '（' + target + '）'
    value = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, value)
    value = html.escape(value)
    value = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', value)
    for index, token in enumerate(tokens):
        value = value.replace(f'CODETOKEN{index}END', token)
    return value.replace('\u2011', '-').replace('\u2013', '-').replace('\u2014', '-')


class Document(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and getattr(flowable, 'outline_level', None) is not None:
            key = flowable.outline_key
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(), key, flowable.outline_level, False)
            if flowable.outline_level == 0:
                self.notify('TOCEntry', (flowable.outline_level, flowable.getPlainText(), self.page, key))


def footer(canvas, document):
    width, height = canvas._pagesize
    canvas.saveState()
    canvas.setFont('CJK', 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(20 * mm, height - 13 * mm, 'RUST COACH  /  技术设计文档')
    canvas.drawRightString(width - 20 * mm, height - 13 * mm, 'v1.2  ·  2026-11-03')
    canvas.setStrokeColor(colors.HexColor('#d9e1e8'))
    canvas.line(20 * mm, 17 * mm, width - 20 * mm, 17 * mm)
    canvas.drawString(20 * mm, 11 * mm, '代码事实优先  |  当前实现与目标设计分开标注')
    canvas.drawRightString(width - 20 * mm, 11 * mm, str(document.page))
    canvas.restoreState()


def cover_footer(canvas, document):
    canvas.saveState()
    canvas.setFillColor(BLUE)
    canvas.rect(20 * mm, 269 * mm, 170 * mm, 3 * mm, fill=1, stroke=0)
    canvas.restoreState()


def heading(value, level=0):
    paragraph = Paragraph(inline(value), styles['H2CN' if level == 0 else 'H3CN'])
    paragraph.outline_level = level
    paragraph.outline_key = re.sub(r'[^a-zA-Z0-9]', '-', value[:12]) + str(len(story))
    return paragraph


doc = Document(str(TEMP / 'design-body.pdf'), pagesize=A4, title='Rust Coach 技术设计文档', author='Rust Coach 项目', subject='六层架构、工程实现、目标设计与开发计划', leftMargin=20 * mm, rightMargin=20 * mm, topMargin=23 * mm, bottomMargin=23 * mm)
body_frame = Frame(20 * mm, 23 * mm, 170 * mm, 251 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
wide_width, wide_height = landscape(A2)
wide_frame = Frame(20 * mm, 23 * mm, wide_width - 40 * mm, wide_height - 46 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
doc.addPageTemplates([
    PageTemplate(id='Cover', frames=[body_frame], onPage=cover_footer, pagesize=A4),
    PageTemplate(id='Body', frames=[body_frame], onPage=footer, pagesize=A4),
    PageTemplate(id='Diagram', frames=[wide_frame], onPage=footer, pagesize=landscape(A2)),
])
story = [Spacer(1, 42 * mm), Paragraph('Rust Coach', styles['CoverTitle']), Paragraph('技术设计文档', styles['CoverTitle']), Spacer(1, 10 * mm), Paragraph('六层架构 · Agent 执行 · 确定性计算<br/>知识与状态 · 质量闭环 · 开发路线', styles['CoverSub']), Spacer(1, 22 * mm)]
story.append(Paragraph('独立阅读版', styles['H3CN']))
story.append(Paragraph('完整收录当前技术设计正文与核心架构总图。已实现能力以实际代码为依据；目标能力明确标注，便于技术评审和面试交流。', styles['BodyCN']))
story.append(Spacer(1, 13 * mm))
story.append(Paragraph('文档版本：v1.2<br/>导出日期：2026-11-03（北京时间）<br/>代码基线：8a32e00592593902555c05e4ee81017f3a8b58de', styles['SmallCN']))
story.append(Spacer(1, 12 * mm))
story.append(Paragraph('阅读提示：正文采用 A4 页面。附录 A 为完整矢量架构大幅页，可在 PDF 阅读器中放大查看。文内文件路径作为来源定位信息保留，阅读本文件不依赖仓库或网络。', styles['SmallCN']))
story.extend([NextPageTemplate('Body'), PageBreak(), Paragraph('目录', styles['H2CN'])])
toc = TableOfContents()
toc.levelStyles = [styles['TOC1'], styles['TOC2']]
story.extend([toc, Spacer(1, 20 * mm), Paragraph('建议阅读顺序', styles['H3CN']), Paragraph('先看附录 A 架构总览与第 1 节能力边界，再读第 3-6 节了解工程协作，最后用第 7-9 节评估质量闭环、当前差距与后续路线。PDF 书签保留各级章节，可直接跳转。', styles['BodyCN']), PageBreak()])
source = (ROOT / 'docs' / 'technical-design.md').read_text()
source = source.replace('![Rust Coach 六层核心架构总览](technical-architecture-target.svg)', '**图 1：完整六层核心架构总览见附录 A（本 PDF 内嵌矢量图）。**')
source = source.replace('[打开完整 SVG](technical-architecture-target.svg)。', '附录 A 已包含完整总览图，无需另行打开 SVG。')
lines = source.splitlines()
index = 1
paragraph_lines = []


def flush():
    if paragraph_lines:
        story.append(Paragraph(inline(' '.join(paragraph_lines)), styles['BodyCN']))
        paragraph_lines.clear()


while index < len(lines):
    line = lines[index]
    if not line.strip():
        flush()
        index += 1
        continue
    if line.startswith('## '):
        flush()
        if line.startswith(('## 9.', '## 10.')):
            story.append(PageBreak())
        story.append(heading(line[3:]))
    elif line.startswith('### '):
        flush()
        if line.startswith('### 10.2 '):
            story.append(PageBreak())
        story.append(heading(line[4:], 1))
    elif line.startswith('|'):
        flush()
        rows = []
        while index < len(lines) and lines[index].startswith('|'):
            cells = [cell.strip() for cell in lines[index].strip('|').split('|')]
            if not all(re.fullmatch(r'[-: ]+', cell) for cell in cells):
                rows.append([Paragraph(inline(cell), styles['CellCN']) for cell in cells])
            index += 1
        count = len(rows[0])
        ratios = [0.18, 0.41, 0.41] if count == 3 else [0.14, 0.29, 0.35, 0.22]
        table = Table(rows, colWidths=[170 * mm * ratio for ratio in ratios], repeatRows=1, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9f0f5')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, 0), 0.7, colors.HexColor('#9db1c2')),
            ('LINEBELOW', (0, 1), (-1, -1), 0.3, colors.HexColor('#dce4eb')),
            ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 10)])
        continue
    elif line.startswith('```'):
        flush()
        code_lines = []
        index += 1
        while index < len(lines) and not lines[index].startswith('```'):
            code_lines.append(html.escape(lines[index]))
            index += 1
        story.append(Paragraph('<br/>'.join(code_lines), styles['CodeCN']))
    elif line.startswith('- '):
        flush()
        story.append(Paragraph('• ' + inline(line[2:]), styles['BodyCN']))
    else:
        paragraph_lines.append(line.strip())
    index += 1
flush()
story.append(heading('10.3 独立阅读版的验收摘要', 1))
story.append(Paragraph('架构总览包含六层、每层三个核心模块及六个共享依赖；组件类型、实现状态和连线类型独立编码。总览实际浏览器渲染检查未发现文字重叠、越界、连线穿节点或交叉。此前源码回归结果为 487 项 Python 测试、16 项前端测试通过；这些结果证明对应测试覆盖范围内的软件行为，不代表游戏规则、视觉识别或任务成功率已通过业务认证。', styles['BodyCN']))
story.append(Paragraph('本 PDF 仅转换排版与来源引用形式，不改变设计内容或将规划能力改为已实现。仓库内参考文档以可读路径标识；源代码用于进一步核查，不是独立阅读的前置条件。', styles['BodyCN']))
story.extend([NextPageTemplate('Diagram'), PageBreak(), heading('附录 A · 六层核心技术架构总览')])
story.append(Spacer(1, 5 * mm))
doc.multiBuild(story)
print(TEMP / 'design-body.pdf')

const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const [runtimeRoot, executablePath, outputDirectory] = process.argv.slice(2);
  if (!runtimeRoot || !executablePath || !outputDirectory) throw new Error('Pass node_modules, Chrome executable and output directory');
  const puppeteer = require(path.join(runtimeRoot, 'puppeteer'));
  const root = path.resolve(__dirname, '..');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'architecture-manifest.json'), 'utf8'));
  const browser = await puppeteer.launch({ executablePath, headless: true, args: ['--no-sandbox'] });
  fs.mkdirSync(outputDirectory, { recursive: true });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: manifest.width, height: manifest.height, deviceScaleFactor: 1 });
    await page.setContent('<meta charset="utf-8"><style>body{margin:0}svg{display:block}</style>' + fs.readFileSync(path.join(root, 'technical-architecture-target.svg'), 'utf8'));
    await page.evaluate(() => document.fonts.ready);
    const rendered = await page.evaluate(() => {
      const boxes = [...document.querySelectorAll('text')].map(element => {
        const box = element.getBBox();
        return { text: element.textContent, x: box.x, y: box.y, width: box.width, height: box.height, component: element.closest('.component')?.id };
      });
      const overlaps = [];
      for (let first = 0; first < boxes.length; first++) {
        for (let second = first + 1; second < boxes.length; second++) {
          const left = boxes[first];
          const right = boxes[second];
          if (Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x) > 2 && Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y) > 2) overlaps.push([left.text, right.text]);
        }
      }
      return { boxes, overlaps };
    });
    const clipped = rendered.boxes.filter(box => box.x < 0 || box.y < 0 || box.x + box.width > manifest.width || box.y + box.height > manifest.height);
    const outOfBounds = rendered.boxes.filter(box => {
      if (!box.component) return false;
      const node = manifest.nodes[box.component];
      return box.x < node.x + 10 || box.y < node.y || box.x + box.width > node.x + node.width - 10 || box.y + box.height > node.y + node.height;
    });
    const segments = manifest.geometry.lines.flatMap(connection => connection.points.slice(1).map((end, index) => ({ id: connection.id, start: connection.points[index], end })));
    const intersects = (segment, box, margin = 0) => segment.start[0] === segment.end[0]
      ? segment.start[0] > box.x + margin && segment.start[0] < box.x + box.width - margin && Math.max(segment.start[1], segment.end[1]) > box.y + margin && Math.min(segment.start[1], segment.end[1]) < box.y + box.height - margin
      : segment.start[1] > box.y + margin && segment.start[1] < box.y + box.height - margin && Math.max(segment.start[0], segment.end[0]) > box.x + margin && Math.min(segment.start[0], segment.end[0]) < box.x + box.width - margin;
    const nodeIntersections = segments.flatMap(segment => manifest.geometry.nodes.filter(box => intersects(segment, box, 1)).map(node => [segment.id, node.id]));
    const lineTextIntersections = segments.flatMap(segment => rendered.boxes.filter(box => intersects(segment, box)).map(box => [segment.id, box.text]));
    const crossings = [];
    for (const horizontal of segments.filter(item => item.start[1] === item.end[1])) {
      for (const vertical of segments.filter(item => item.start[0] === item.end[0])) {
        if (horizontal.id === vertical.id) continue;
        if (vertical.start[0] > Math.min(horizontal.start[0], horizontal.end[0]) && vertical.start[0] < Math.max(horizontal.start[0], horizontal.end[0]) && horizontal.start[1] > Math.min(vertical.start[1], vertical.end[1]) && horizontal.start[1] < Math.max(vertical.start[1], vertical.end[1])) crossings.push([horizontal.id, vertical.id]);
      }
    }
    const nonOrthogonal = segments.filter(segment => segment.start[0] !== segment.end[0] && segment.start[1] !== segment.end[1]);
    const report = { nodes: Object.keys(manifest.nodes).length, relations: manifest.relations.length, textCount: rendered.boxes.length, overlaps: rendered.overlaps, clipped, outOfBounds, nodeIntersections, lineTextIntersections, crossings, nonOrthogonal };
    fs.writeFileSync(path.join(outputDirectory, 'architecture-qa.json'), JSON.stringify(report, null, 2));
    await page.screenshot({ path: path.join(outputDirectory, 'architecture-full.png'), fullPage: true });
    await page.addStyleTag({ content: 'svg{width:1500px;height:auto}' });
    await page.setViewport({ width: 1500, height: manifest.height / 2, deviceScaleFactor: 1 });
    await page.screenshot({ path: path.join(outputDirectory, 'architecture-overview.png'), fullPage: true });
    console.log(JSON.stringify(report, null, 2));
    if ([rendered.overlaps, clipped, outOfBounds, nodeIntersections, lineTextIntersections, crossings, nonOrthogonal].some(items => items.length)) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });

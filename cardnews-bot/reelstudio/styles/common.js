// 릴스 스타일 페이지가 같이 쓰는 도구: 내용 불러오기, 글꼴, 한글 줄바꿈, 안전 영역, 자막.
// 페이지는 window.render(t)로 t초의 화면을 결정적으로 그린다 (lemo-opuscar의 페이지 규약).
import { clamp, lerp, seg, ss, eo, eio, ei, back, mulberry, hash } from '/core/lib.js';
export { clamp, lerp, seg, ss, eo, eio, ei, back, mulberry, hash };

export const W = 1080, H = 1920;
// 인스타 릴스·유튜브 쇼츠 앱 버튼에 가려지는 곳: 위 250, 아래 360, 오른쪽 130(버튼). 글자와 상자는 이 안쪽에만 둔다.
export const SAFE = { top: 262, bottom: 1920 - 372, left: 70, right: 1080 - 142 };
export const SAFE_W = SAFE.right - SAFE.left;   // 868

export async function loadContent() {
  const res = await fetch('content.json');
  if (!res.ok) throw new Error('content.json ' + res.status);
  return res.json();
}

export function setupCanvas() {
  const c = document.getElementById('c');
  c.width = W; c.height = H;
  return { c, ctx: c.getContext('2d') };
}

// fonts: [{family, url?, weight, css?}]  url는 content.json 옆 fonts/ 폴더 파일. css는 <link>로 이미 걸어 둔 글꼴.
export async function loadFonts(fonts, sampleText) {
  for (const f of fonts) {
    if (f.url) {
      const ff = new FontFace(f.family, `url(${f.url})`, { weight: String(f.weight) });
      document.fonts.add(await ff.load());
    } else {
      await document.fonts.load(`${f.weight} 60px "${f.family}"`, sampleText);
    }
  }
  await document.fonts.ready;
}

export function allText(content) {
  const parts = [content.title, content.handle, content.channel || ''];
  for (const s of content.scenes) {
    parts.push(s.heading, s.body, s.highlight, s.label || '');
    for (const b of s.bullets || []) parts.push(b);
    for (const u of s.subs || []) parts.push(u.text);
  }
  return parts.join('') + '0123456789,.:%/~·…!?+-()[]ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
}

export function sceneAt(content, t) {
  const sc = content.scenes;
  let i = 0;
  for (let k = 0; k < sc.length; k++) if (t >= sc[k].t0) i = k;
  const s = sc[i];
  return { i, s, lt: t - s.t0, dur: s.t1 - s.t0, prog: clamp((t - s.t0) / (s.t1 - s.t0)) };
}

export function subAt(scene, t) {
  for (const u of scene.subs || []) if (t >= u.t0 && t < u.t1) return { u, lt: t - u.t0, dur: u.t1 - u.t0 };
  return null;
}

// 한글은 글자 단위로 줄을 바꾸되 공백이 가까우면 공백에서 바꾼다.
export function wrap(ctx, text, maxW) {
  const out = [];
  for (const para of String(text || '').split('\n')) {
    let line = '';
    for (const ch of [...para]) {
      const test = line + ch;
      if (ctx.measureText(test).width > maxW && line) {
        const sp = line.lastIndexOf(' ');
        if (sp > line.length * 0.5 && ch !== ' ') { out.push(line.slice(0, sp)); line = line.slice(sp + 1) + ch; }
        else { out.push(line); line = ch === ' ' ? '' : ch; }
      } else line = test;
    }
    if (line) out.push(line);
  }
  return out;
}

// 줄 수 제한 안에서 가장 큰 글자 크기를 고른다. 반환: {size, lines}
export function fitText(ctx, text, { family, weight, maxW, maxLines, sizes }) {
  for (const size of sizes) {
    ctx.font = `${weight} ${size}px "${family}"`;
    const lines = wrap(ctx, text, maxW);
    if (lines.length <= maxLines) return { size, lines };
  }
  const size = sizes[sizes.length - 1];
  ctx.font = `${weight} ${size}px "${family}"`;
  return { size, lines: wrap(ctx, text, maxW).slice(0, maxLines) };
}

export function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

// "15만 원", "3.5%", "1,200" 같은 문구에서 숫자 부분만 굴려서 보여 준다 (p: 0→1)
export function rollNumber(text, p) {
  return String(text).replace(/\d[\d,]*(\.\d+)?/g, m => {
    const dec = (m.split('.')[1] || '').length;
    const v = parseFloat(m.replace(/,/g, '')) * p;
    const s = v.toFixed(dec);
    const [a, b] = s.split('.');
    const withComma = m.includes(',') ? a.replace(/\B(?=(\d{3})+(?!\d))/g, ',') : a;
    return b !== undefined ? `${withComma}.${b}` : withComma;
  });
}

// 자막(내레이션 글): 앱 아래 버튼 영역(아래 360px) 위쪽, 오른쪽 버튼 영역(130px) 왼쪽에 둔다.
export function drawSubtitle(ctx, content, t, opt = {}) {
  const { s } = sceneAt(content, t);
  const hit = subAt(s, t);
  if (!hit) return;
  const o = Object.assign({ family: 'Pretendard', weight: 700, size: 50, color: '#fff', bg: 'rgba(0,0,0,.62)', radius: 18, padX: 28, padY: 16, bottom: SAFE.bottom - 40, lh: 1.32, stroke: null }, opt);
  const a = ss(hit.lt / 0.12) * (1 - ss((hit.lt - (hit.dur - 0.08)) / 0.08));
  ctx.save();
  ctx.globalAlpha = a;
  ctx.font = `${o.weight} ${o.size}px "${o.family}"`;
  const lines = wrap(ctx, hit.u.text, SAFE_W - o.padX * 2);
  const lh = o.size * o.lh;
  const boxH = lines.length * lh + o.padY * 2;
  const w = Math.max(...lines.map(l => ctx.measureText(l).width)) + o.padX * 2;
  const cx = (SAFE.left + SAFE.right) / 2;
  const y0 = o.bottom - boxH + (1 - a) * 12;
  if (o.bg) { ctx.fillStyle = o.bg; roundRect(ctx, cx - w / 2, y0, w, boxH, o.radius); ctx.fill(); }
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  lines.forEach((l, k) => {
    const y = y0 + o.padY + lh * (k + 0.5);
    if (o.stroke) { ctx.lineWidth = o.stroke.w; ctx.strokeStyle = o.stroke.c; ctx.lineJoin = 'round'; ctx.strokeText(l, cx, y); }
    ctx.fillStyle = o.color; ctx.fillText(l, cx, y);
  });
  ctx.restore();
}

// 스타일 페이지가 마지막에 부르는 한 줄: 내용을 읽고, 글꼴을 올리고, window.render를 등록한다.
export async function start({ fonts, draw }) {
  const content = await loadContent();
  const { ctx } = setupCanvas();
  await loadFonts(fonts, allText(content));
  window.DUR = content.duration;
  window.render = t => draw(ctx, content, t);
  window.TEXTS = t => {
    const { s } = sceneAt(content, t);
    return [{ id: 'heading' + s.index, text: s.heading, x0: SAFE.left, y0: SAFE.top, x1: SAFE.right, y1: SAFE.bottom }];
  };
  window.render(0);
  window.READY = true;
}

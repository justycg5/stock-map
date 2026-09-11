/* A股云图 · 本地优化版
 * 自绘 canvas Treemap：无上限缩放、动态字号标签、悬停强制显示、下钻/面包屑、搜索定位。
 */
"use strict";

// ---------- DOM ----------
const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const tooltip = document.getElementById("tooltip");
const crumbEl = document.getElementById("crumb");
const searchEl = document.getElementById("search");
const dropdownEl = document.getElementById("dropdown");
const statTime = document.getElementById("statTime");
const statCount = document.getElementById("statCount");
const btnRefresh = document.getElementById("btnRefresh");
const autoRefreshEl = document.getElementById("autoRefresh");

// ---------- 常量 ----------
const BG = "#0e1117";
const GRID = "#1a2029";
const MAX_SCALE = 1e6;          // 缩放上限：约 100 万倍，等效无上限
const AUTO_REFRESH_MS = 60000;  // 自动刷新间隔
const QUOTE_BATCH = 500;         // 实时行情每批最多 500 个代码（服务端上限，>500 返回 503）
const QUOTE_CONCURRENCY = 6;     // 并发批数
const QUOTE_INTERVAL_MS = 8000;  // 实时刷新间隔（秒级，交易时段）
const QUOTE_URL = "https://push2delay.eastmoney.com/api/qt/ulist.np/get";
// ---------- 区间涨跌幅（一期：预设 6 档，字段一次请求全部取得） ----------
const RANGES = [
  { id: "day", label: "当日", field: "pct" },
  { id: "d5", label: "近5日", field: "pct5" },
  { id: "d10", label: "近10日", field: "pct10" },
  { id: "d20", label: "近20日", field: "pct20" },
  { id: "d60", label: "近60日", field: "pct60" },
  { id: "ytd", label: "年初至今", field: "pctYTD" },
];
const RANGE_FIELDS = RANGES.map(r => r.field);
let currentRange = "day";
let rangeAppliedFromUrl = false;  // URL ?range= 参数只应用一次，避免覆盖手动切换
let tooltipLocked = false;         // 鼠标移入 tooltip 时锁定位置，便于点击加自选按钮
let flashNode = null;              // 右键加自选金边反馈的节点
let flashUntil = 0;

// ---------- v20 全球指数地图视图 ----------
let view = "a";                    // "a" = A股云图 / "global" = 全球指数 / "futures" = 期货
let fItems = [];                   // futures.json 期货列表
let fLiveTimer = null;             // 期货独立实时定时器
let gChart = null;                 // ECharts 实例（懒初始化）
let gWorld = null;                 // 世界地图 GeoJSON
let gItems = [];                   // global.json 指数列表
let gCountryMap = new Map();       // 区域英文名 -> 指数数组
let gPanelOpen = false;
function rangeField() {
  const r = RANGES.find(x => x.id === currentRange);
  return r ? r.field : "pct";
}
function rangePctOf(node) {
  if (!node) return null;
  const v = node[rangeField()];
  return (v === null || v === undefined || isNaN(v)) ? null : v;
}
const FONT_FAMILY = "'Microsoft YaHei','PingFang SC',system-ui,sans-serif";

// ---------- 状态 ----------
let W = 0, H = 0, dpr = 1;
let scale = 1, offsetX = 0, offsetY = 0, baseScale = 1;
let WORLD_W = 1;   // 世界宽度（横屏=1 正方形画布；竖屏=W/H<1 → 宽满屏）
let WORLD_H = 1;   // v91 世界高度：横屏恒 1；竖屏由 placeGroups 行高下限决定（组格加高，允许整图超出屏长，拖拽/双击空白浏览）
let _viewAdjusted = false;
let root = null;        // 树根
let current = null;     // 当前视图节点
let layoutCache = null; // 当前层布局缓存 [{node, x, y, w, h}]
let hover = null;       // 悬停的节点
let highlight = null;   // 搜索定位高亮 {code, t0}
let fetchedAt = "";
let totalStocks = 0;
let totalIndustries = 0;
let autoTimer = null;

// ---------- 工具函数 ----------
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

function fmtNum(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return v.toFixed(digits === undefined ? 2 : digits);
}

function fmtYi(v) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return (v / 1e8).toFixed(1) + " 亿";
}

// 涨跌配色：A股红涨绿跌
function pctColor(pct) {
  if (pct === null || pct === undefined || isNaN(pct)) return "#4a5568";
  if (pct === 0) return "#6b7280";
  const t = clamp(Math.abs(pct) / 7, 0, 1);
  const s = 52 + 38 * t;
  const l = pct > 0 ? 50 - 15 * t : 48 - 13 * t;
  const h = pct > 0 ? 0 : 140;
  return "hsl(" + h + "," + s + "%," + l + "%)";
}

function pctTextColor(pct) {
  if (pct === null || pct === undefined || isNaN(pct)) return "#9aa3b2";
  return pct >= 0 ? "#ff8a8f" : "#63d6a0";
}

// 网格平铺布局：等面积方形格子（所有层级使用，保证每个单元独立可见）
function layoutGrid(children, x, y, w, h, sortBy) {
  const items = [];
  for (let i = 0; i < children.length; i++) {
    items.push({ i: i, v: children[i].value > 0 ? children[i].value : 0, node: children[i] });
  }
  if (items.length === 0) return [];
  if (sortBy) items.sort(sortBy);
  else items.sort((a, b) => b.v - a.v);
  const k = items.length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(k * (w / h))));
  const rows = Math.ceil(k / cols);
  const cw = w / cols, ch = h / rows;
  const out = [];
  for (let n = 0; n < k; n++) {
    const c = n % cols, r = Math.floor(n / cols);
    out.push({ i: items[n].i, x: x + c * cw, y: y + r * ch, w: cw, h: ch });
  }
  return out;
}

// ---------- 数据 → 树 ----------
function weightedPct(stocks, field) {
  const f = field || "pct";
  let wsum = 0, msum = 0;
  for (const s of stocks) {
    if (s[f] !== null && s[f] !== undefined && s.mcap) { wsum += s[f] * s.mcap; msum += s.mcap; }
  }
  return msum > 0 ? wsum / msum : null;
}

// 按最新成分股重算节点全部区间涨跌幅（市值加权）
function recalcNodePcts(node, children) {
  node.pct = weightedPct(children);
  node.pct5 = weightedPct(children, "pct5");
  node.pct10 = weightedPct(children, "pct10");
  node.pct20 = weightedPct(children, "pct20");
  node.pct60 = weightedPct(children, "pct60");
  node.pctYTD = weightedPct(children, "pctYTD");
}

function buildTree(payload) {
  // v89：三层树（一级 → 二级 → 个股）；sectors 由 fetch 端生成，缺失时回退二级两层
  if (payload.sectors && payload.sectors.length) {
    const mkStock = st => ({
      name: st.name, code: st.code, industry: st.industry || "其他",
      value: st.mcap && st.mcap > 0 ? st.mcap : 0.1,
      price: st.price, pct: st.pct, chg: st.chg,
      amount: st.amount, mcap: st.mcap,
      peTTM: st.peTTM, rev: st.rev, revYoy: st.revYoy, np: st.np, npYoy: st.npYoy,
      pct5: st.pct5, pct10: st.pct10, pct20: st.pct20, pct60: st.pct60, pctYTD: st.pctYTD,
      leaf: true, parent: null,
    });
    const mkInd = ind => {
      const stocks = ind.stocks.map(mkStock);
      const node = {
        name: ind.name, value: ind.total_mcap && ind.total_mcap > 0 ? ind.total_mcap : 0.1,
        pe: ind.pe,
        pct: weightedPct(ind.stocks), pct5: weightedPct(ind.stocks, "pct5"),
        pct10: weightedPct(ind.stocks, "pct10"), pct20: weightedPct(ind.stocks, "pct20"),
        pct60: weightedPct(ind.stocks, "pct60"), pctYTD: weightedPct(ind.stocks, "pctYTD"),
        children: stocks, leaf: false, parent: null,
      };
      for (const s of stocks) s.parent = node;
      return node;
    };
    // v97：sectors.children 仅存行业名（行业明细与 industries 去重，data.json 体积减半），
    // 按名反查 payload.industries 建树；兼容旧格式（children 内嵌行业对象）
    const indByName = new Map((payload.industries || []).map(i => [i.name, i]));
    const secs = payload.sectors.map(sec => {
      const indNodes = sec.children
        .map(c => (typeof c === "string" ? indByName.get(c) : c))
        .filter(Boolean)
        .map(mkInd);
      const node = {
        name: sec.name, value: sec.total_mcap && sec.total_mcap > 0 ? sec.total_mcap : 0.1,
        pe: sec.pe,
        pct: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), [])),
        pct5: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), []), "pct5"),
        pct10: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), []), "pct10"),
        pct20: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), []), "pct20"),
        pct60: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), []), "pct60"),
        pctYTD: weightedPct(indNodes.reduce((a, x) => (a.push.apply(a, x.children), a), []), "pctYTD"),
        children: indNodes, leaf: false, parent: null,
      };
      for (const c of indNodes) c.parent = node;
      return node;
    });
    const allStocks3 = secs.reduce((a, s) => a.concat(s.children.reduce((b, ind) => b.concat(ind.children), [])), []);
    const rootNode3 = {
      name: "全部A股",
      value: secs.reduce((s, x) => s + x.value, 0),
      pct: weightedPct(allStocks3), pct5: weightedPct(allStocks3, "pct5"),
      pct10: weightedPct(allStocks3, "pct10"), pct20: weightedPct(allStocks3, "pct20"),
      pct60: weightedPct(allStocks3, "pct60"), pctYTD: weightedPct(allStocks3, "pctYTD"),
      children: secs, leaf: false, parent: null,
    };
    for (const s of secs) s.parent = rootNode3;
    rootNode3._sectors = secs;
    return rootNode3;
  }
  const inds = payload.industries.map(ind => {
    const stocks = ind.stocks.map(st => ({
      name: st.name, code: st.code, industry: st.industry || "其他",
      value: st.mcap && st.mcap > 0 ? st.mcap : 0.1,
      price: st.price, pct: st.pct, chg: st.chg,
      amount: st.amount, mcap: st.mcap,
      peTTM: st.peTTM, rev: st.rev, revYoy: st.revYoy, np: st.np, npYoy: st.npYoy,
      pct5: st.pct5, pct10: st.pct10, pct20: st.pct20, pct60: st.pct60, pctYTD: st.pctYTD,
      leaf: true, parent: null,
    }));
    const node = {
      name: ind.name,
      value: ind.total_mcap && ind.total_mcap > 0 ? ind.total_mcap : 0.1,
      pe: ind.pe,
      pct: weightedPct(ind.stocks),
      pct5: weightedPct(ind.stocks, "pct5"),
      pct10: weightedPct(ind.stocks, "pct10"),
      pct20: weightedPct(ind.stocks, "pct20"),
      pct60: weightedPct(ind.stocks, "pct60"),
      pctYTD: weightedPct(ind.stocks, "pctYTD"),
      children: stocks, leaf: false, parent: null,
    };
    for (const s of stocks) s.parent = node;
    return node;
  });
  const allStocks = payload.industries.reduce((a, ind) => a.concat(ind.stocks), []);
  const rootNode = {
    name: "全部A股",
    value: inds.reduce((s, x) => s + x.value, 0),
    pct: weightedPct(allStocks),
    pct5: weightedPct(allStocks, "pct5"),
    pct10: weightedPct(allStocks, "pct10"),
    pct20: weightedPct(allStocks, "pct20"),
    pct60: weightedPct(allStocks, "pct60"),
    pctYTD: weightedPct(allStocks, "pctYTD"),
    children: inds, leaf: false, parent: null,
  };
  for (const ind of inds) ind.parent = rootNode;
  return rootNode;
}

// ---------- 视图控制 ----------
function resetView() {
  // v94 移动端（竖屏）：宽满屏（云图宽度=屏幕宽度）、顶部对齐菜单栏下缘；下方超出屏幕的部分拖拽/双击浏览
  if (WORLD_W < 1) {
    scale = W / WORLD_W;
    offsetX = 0;
    offsetY = 0;
    return;
  }
  const fitW = W / WORLD_W;
  const fitH = H / WORLD_H;
  scale = Math.min(baseScale, fitW, fitH === Infinity ? baseScale : fitH);
  if (WORLD_H <= 1) scale = baseScale;
  offsetX = (W - WORLD_W * scale) / 2;
  offsetY = Math.min(0, (H - WORLD_H * scale) / 2);   // 超长时顶对齐（从内容顶部开始看）
}

const HEADER_H = 0.036;   // 组标头预留带（世界单位，placeGroups 与 draw 共用）

function relayout() {
  const kids = current.children;
  // v90 根视图：129 个二级行业按申万一级分组展示（外层组区 + 组内二级格），保留涨跌分区语义
  if (current === root && root._sectors) {
    const pctOf = c => rangePctOf(c);
    // 组（一级）按加权涨跌幅分涨跌两区，区内按强度排（与旧板块排序语义一致，单位变为组）
    const gs = root._sectors;
    const rf = rangeField();   // v90：分区/排序随区间联动（当日/5日/…/YTD）
    const gv = g => { const v = g[rf]; return (v === null || v === undefined || isNaN(v)) ? null : v; };
    const up = [], down = [];
    for (const g of gs) {
      const v = gv(g);
      if (v !== null && v < 0) down.push(g);
      else up.push(g);
    }
    const cmpUp = (a, b) => (gv(b) || 0) - (gv(a) || 0);
    // 下跌区：跌幅小（更接近 0）靠前，跌幅大靠后
    const cmpDown = (a, b) => (gv(b) || 0) - (gv(a) || 0);
    up.sort(cmpUp); down.sort(cmpDown);
    const kU = up.length, kD = down.length;
    const GAP = 0.006;
    const total = kU + kD;
    const hU = total > 0 ? (kU / total) * (1 - GAP) : 1 - GAP;
    layoutCache = [];
    // 组区外层布局：面积 ∝ 组内二级行业数（layoutGrid 按第三参对象 value；这里构造 wrapper）
    const gsWrap = arr => arr.map(g => ({ g: g, value: g.children.length }));
    const placeGroups = (arr, x, y, w, h, cmp) => {
      const items = gsWrap(arr);
      items.sort((a, b) => cmp(a.g, b.g));
      const k = items.length;
      if (!k) return;
      let cols = Math.max(1, Math.ceil(Math.sqrt(k * (w / h))));
      if (WORLD_W < 1) cols = Math.min(cols, 3);   // 竖屏手机：每行最多 3 组（屏幕窄）
      const rows = Math.ceil(k / cols);
      const cw = w / cols;
      // v92 移动端：行高按内容分配（每行的组 = 同 r 下 c 0..cols-1 连续 idx）
      const rowItems = [];   // rowItems[r] = items 数组（该行实际组）
      for (let r = 0; r < rows; r++) {
        const row = [];
        for (let c = 0; c < cols; c++) {
          const idx = r * cols + c;
          if (idx < items.length) row.push(items[idx]);
        }
        if (row.length) rowItems.push(row);
      }
      const rowH = (rr, row) => {
        // 行高 ∝ 该行最大组内行数（srows）；含 2 行二级格的组占更高；单行组占低
        const ghhMax = h - HEADER_H * rowItems.length;   // 全部行扣除各自标头后总可用高
        let maxS = 1;
        for (const it of row) {
          const kn = it.g.children.length;
          if (!kn) continue;
          const sc = Math.max(1, Math.ceil(Math.sqrt(kn * (cw / Math.max(ghhMax / rowItems.length, 0.01)))));
          maxS = Math.max(maxS, Math.ceil(kn / sc));
        }
        return maxS;
      };
      // 行高权重：srows 数（含2行的组权重2，单行权重1）
      const wRow = rowItems.map((row, rr) => rowH(rr, row));
      const wSum = wRow.reduce((a, b) => a + b, 0);
      let unit = (h - HEADER_H * rowItems.length) / wSum;   // 每"行单位"高度
      // v92 移动端：行单位高下限（屏幕 px 换算世界单位），保证组内格子可读；总高超出屏 → WORLD_H>1，可拖拽浏览
      if (WORLD_W < 1) {
        const MIN_ROW_PX = 46;
        const minUnitWorld = MIN_ROW_PX / scale;
        if (unit < minUnitWorld) unit = minUnitWorld;
      }
      let yCursor = 0;
      for (let r = 0; r < rowItems.length; r++) {
        const row = rowItems[r];
        const ch = HEADER_H + wRow[r] * unit;                 // 本行总高 = 标头 + 行单位高×权重
        const ghh = ch - HEADER_H;                            // 本行组内容高
        for (let c = 0; c < row.length; c++) {
          const gx = x + c * cw, gy = y + yCursor + HEADER_H;
          const secs = row[c].g.children.slice();
          secs.sort((a, b) => {
            const pa = pctOf(a), pb = pctOf(b);
            if (pa === null) return 1; if (pb === null) return -1;
            return pb - pa;
          });
          const kn = secs.length;
          if (!kn) continue;
          const scols = Math.max(1, Math.ceil(Math.sqrt(kn * (cw / ghh))));   // v92：组内排列与桌面同公式（自然自适应组宽）
          const srows = Math.ceil(kn / scols);
          const sw = cw / scols, sh = ghh / srows;
          const fullRows = Math.floor(kn / scols);      // 满行数
          const lastCnt = kn - fullRows * scols;        // 末行格数（0=无末行残留）
          let si = 0;
          for (let sr = 0; sr < srows; sr++) {
            // v90 方案C：末行不满时拉宽占满组宽（消除组内空白）
            const inLast = sr === srows - 1 && lastCnt > 0;
            const rCols = inLast ? lastCnt : scols;
            const rW = inLast ? cw / lastCnt : sw;
            for (let sc = 0; sc < rCols && si < kn; sc++, si++) {
              layoutCache.push({
                node: secs[si],
                x: gx + sc * rW, y: gy + sr * sh, w: rW, h: sh,
                _grp: row[c].g,   // 归属组（标头/边框用）
              });
            }
          }
        }
        yCursor += ch;
      }
      return yCursor;
    };
    let worldH = 1;
    if (kU > 0) worldH = placeGroups(up, 0, 0, WORLD_W, hU, cmpUp) || hU;
    if (kD > 0) {
      const dH = placeGroups(down, 0, worldH + GAP, WORLD_W, Math.max(worldH + 0.3, 1) - worldH - GAP, cmpDown);
      worldH = Math.max(worldH + GAP + (dH || 0), worldH);
    }
    WORLD_H = Math.max(1, worldH);
    // 首次布局后校正视口（竖屏超长场景）：只在 resetView 语义下自动调用一次
    if (WORLD_W < 1 && WORLD_H > 1 && !_viewAdjusted) {
      _viewAdjusted = true;
      resetView();
    }
    return;
  }
  // 其他层级（行业成分股等）：按涨跌幅降序网格平铺（用户偏好：二级页也按涨跌幅排序）
  const pctSort = (a, b) => {
    const pa = rangePctOf(a.node);
    const pb = rangePctOf(b.node);
    if (pa === null) return 1;
    if (pb === null) return -1;
    return pb - pa;
  };
  layoutCache = layoutGrid(kids, 0, 0, WORLD_W, 1, pctSort)
    .map(r => ({ node: kids[r.i], x: r.x, y: r.y, w: r.w, h: r.h }));
}

function zoomToNode(rect, fitRatio) {
  const fit = clamp(fitRatio, 0.05, 0.95);
  const s = fit * Math.min(W / rect.w, H / rect.h);
  scale = clamp(s, baseScale * 0.5, MAX_SCALE);
  offsetX = W / 2 - (rect.x + rect.w / 2) * scale;
  offsetY = H / 2 - (rect.y + rect.h / 2) * scale;
}

function drillInto(node) {
  if (!node || node.leaf) return;
  current = node;
  relayout();
  const rect = rectOfNode(node);
  if (rect) zoomToNode(rect, 0.9);
  else resetView();
  renderCrumb();
  draw();
}

function goUp() {
  if (!current.parent) { resetView(); draw(); return; }
  current = current.parent;
  relayout();
  const rect = rectOfNode(current);
  if (rect) zoomToNode(rect, 0.9);
  else resetView();
  renderCrumb();
  draw();
}

function rectOfNode(node) {
  if (!layoutCache) return null;
  for (const r of layoutCache) if (r.node === node) return r;
  return null;
}

function renderCrumb() {
  const chain = [];
  let n = current;
  while (n) { chain.unshift(n); n = n.parent; }
  let html = "";
  for (let i = 0; i < chain.length; i++) {
    if (i > 0) html += '<span class="crumb-sep">›</span>';
    const cls = i === chain.length - 1 ? "crumb-item current" : "crumb-item";
    const label = chain[i].name + (i === 0 && totalStocks ? " (" + totalStocks + ")" : "");
    html += '<span class="crumb-item ' + (i === chain.length - 1 ? "current" : "") +
      '" data-idx="' + i + '">' + label + "</span>";
  }
  crumbEl.innerHTML = html;
  crumbEl.querySelectorAll(".crumb-item").forEach(el => {
    el.addEventListener("click", () => {
      const idx = parseInt(el.dataset.idx, 10);
      current = chain[idx];
      relayout();
      const rect = rectOfNode(current);
      if (rect) zoomToNode(rect, 0.9); else resetView();
      renderCrumb(); draw();
    });
  });
}

// ---------- 绘制 ----------
function worldToScreen(r) {
  return {
    x: r.x * scale + offsetX,
    y: r.y * scale + offsetY,
    w: r.w * scale,
    h: r.h * scale,
  };
}

function draw() {
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, W, H);
  if (!layoutCache) { drawEmpty(); return; }

  // 视口剔除 + 绘制方块
  const vis = [];
  for (const r of layoutCache) {
    const s = worldToScreen(r);
    if (s.x > W || s.y > H || s.x + s.w < 0 || s.y + s.h < 0) continue;
    vis.push({ r: r, s: s });
    ctx.fillStyle = pctColor(rangePctOf(r.node));
    ctx.fillRect(s.x + 0.5, s.y + 0.5, Math.max(s.w - 1, 0), Math.max(s.h - 1, 0));
  }

  // 网格线（方块间隙）
  ctx.strokeStyle = BG;
  ctx.lineWidth = 1;
  for (const v of vis) {
    ctx.strokeRect(v.s.x + 0.5, v.s.y + 0.5, Math.max(v.s.w - 1, 0), Math.max(v.s.h - 1, 0));
  }

  // v90 根视图分组：组边界 + 组名标头（_grp 相同的格子构成一个组区）
  if (current === root && root._sectors && vis.length && vis[0].r._grp) {
    const byGrp = new Map();
    for (const v of vis) {
      const g = v.r._grp;
      if (!g) continue;
      let b = byGrp.get(g);
      if (!b) { b = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity, g: g }; byGrp.set(g, b); }
      b.x0 = Math.min(b.x0, v.s.x); b.y0 = Math.min(b.y0, v.s.y);
      b.x1 = Math.max(b.x1, v.s.x + v.s.w); b.y1 = Math.max(b.y1, v.s.y + v.s.h);
    }
    ctx.save();
    for (const b of byGrp.values()) {
      const label = b.g.name;
      const gp = b.g[rangeField()];
      const hasGp = gp !== undefined && gp !== null;
      // 标头尺寸（带内底部对齐；字号随缩放自适应，钳制保底可读）
      const bandPx = HEADER_H * scale;                            // 预留带总高（屏幕 px）
      // v92 缩小适配：框边距/线宽随 scale 收敛（scale<400 线性衰减），缩小时相邻组框不再互相遮挡
      const shrink = Math.min(1, scale / 400);
      const pad = 2.5 * shrink;
      const lw1 = 4 * shrink, lw2 = Math.max(0.5, 1.6 * shrink);
      const bh = Math.max(12, Math.min(bandPx - 6, 24));          // 标题条高（带内可容纳，随缩放收敛）
      const fs1 = Math.max(9, Math.min(15, bh * 0.62));           // 组名字号
      const fs2 = Math.max(8.5, Math.min(13, bh * 0.53));         // 涨跌幅字号
      const pw = hasGp ? Math.max(44, Math.min(74, bh * 2.9)) : 0; // 涨跌幅色块宽
      const hy = b.y0 - pad - bh;                                 // 标题条底=本组格顶-pad
      // 组框：包裹「标题条 + 格子区」整体，四边封闭（标题条通栏宽）
      const bx = b.x0 - pad, by = hy - pad;                        // 框顶=标题条上缘
      const bw = (b.x1 - b.x0) + pad * 2, bh2 = (b.y1 - hy) + pad * 2; // 框高=标题条+格子区
      // 标题条底色（通栏）
      ctx.fillStyle = "rgba(13,17,27,0.96)";
      ctx.fillRect(bx, hy, bw, bh);
      ctx.fillStyle = "rgba(190,205,230,0.9)";
      ctx.fillRect(bx, hy, 3, bh);   // 左侧亮色竖条
      ctx.font = "700 " + fs1.toFixed(1) + "px " + FONT_FAMILY;
      const tw = ctx.measureText(label).width;
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillStyle = "#f2f6fd";
      ctx.fillText(label, bx + 9, hy + bh / 2 + 0.5);
      if (hasGp) {
        const up = gp >= 0;
        const ps = (up ? "+" : "") + Number(gp).toFixed(2) + "%";
        const px0 = bx + tw + 14;
        ctx.fillStyle = up ? "rgba(224,49,49,0.95)" : "rgba(15,143,91,0.95)";
        ctx.fillRect(px0, hy + 3, pw, bh - 6);
        ctx.fillStyle = "#fff";
        ctx.font = "700 " + fs2.toFixed(1) + "px " + FONT_FAMILY;
        ctx.textAlign = "center";
        ctx.fillText(ps, px0 + pw / 2, hy + bh / 2 + 0.5);
        ctx.textAlign = "left";
      }
      // 标题条与格子区之间：弱分隔线（视觉分层，不抢组框主线条）
      ctx.strokeStyle = "rgba(190,205,230,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(bx, hy + bh);
      ctx.lineTo(bx + bw, hy + bh);
      ctx.stroke();
      // 组边界：两层描边（外层深色宽线 + 内层亮色细线），包裹整体、实线醒目
      ctx.strokeStyle = "rgba(2,6,15,0.9)";
      ctx.lineWidth = lw1;
      ctx.strokeRect(bx, by, bw, bh2);
      ctx.strokeStyle = "rgba(190,205,230,0.85)";
      ctx.lineWidth = lw2;
      ctx.strokeRect(bx, by, bw, bh2);
    }
    ctx.restore();
  }

  // 标签
  for (const v of vis) drawLabel(v.r.node, v.s, v.r.node === hover);

  // 备注编辑按钮（仅二级视图 + 叶子 + 格子长边≥520px）：右上角圆角按钮，金色显眼
  if (current && current !== root) {
    ctx.save();
    ctx.font = "bold 13px " + FONT_FAMILY;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (const v of vis) {
      const n = v.r.node;
      if (!n.leaf || !n.code) continue;
      if (Math.max(v.s.w, v.s.h) < NOTE_BTN_MIN_LEN) continue;
      const has = !!noteGet(n.code);
      const b = noteBtnRect(v.s);
      ctx.fillStyle = has ? "#ffd94a" : "#ff9f43";
      rrPath(ctx, b.x, b.y, b.w, b.h, 6);
      ctx.fill();
      ctx.fillStyle = "#1b2536";
      ctx.textAlign = "center";
      ctx.fillText("✎", b.x + b.w / 2, b.y + b.h / 2 + 0.5);
      ctx.textAlign = "left";
    }
    ctx.restore();
  }

  // 高亮（搜索定位）
  if (highlight) {
    const now = performance.now();
    const dt = (now - highlight.t0) / 1000;
    if (dt > 2.5) highlight = null;
    else {
      const node = highlight.code ? findNodeByCode(highlight.code) : findNodeByName(highlight.name);
      if (node) {
        const r = rectOfNode(node);
        if (r) {
          const s = worldToScreen(r);
          const alpha = 0.6 + 0.4 * Math.sin(dt * 10);
          ctx.strokeStyle = "rgba(255,255,255," + alpha + ")";
          ctx.lineWidth = 3;
          ctx.strokeRect(s.x + 1.5, s.y + 1.5, Math.max(s.w - 3, 0), Math.max(s.h - 3, 0));
        }
      }
    }
  }

  // 悬停边框
  if (hover) {
    const r = rectOfNode(hover);
    if (r) {
      const s = worldToScreen(r);
      ctx.strokeStyle = "rgba(255,255,255,.85)";
      ctx.lineWidth = 2;
      ctx.strokeRect(s.x + 1, s.y + 1, Math.max(s.w - 2, 0), Math.max(s.h - 2, 0));
    }
  }

  // 右键加自选金边反馈
  if (flashNode && flashUntil > performance.now()) {
    const r = rectOfNode(flashNode);
    if (r) {
      const s = worldToScreen(r);
      ctx.strokeStyle = "#f5c542";
      ctx.lineWidth = 2.5;
      ctx.strokeRect(s.x + 1, s.y + 1, Math.max(s.w - 2, 0), Math.max(s.h - 2, 0));
    }
  }
}

function drawEmpty() {
  ctx.fillStyle = "#2a3242";
  ctx.font = "14px " + FONT_FAMILY;
  ctx.textAlign = "center";
  ctx.fillText("数据加载中…（首次运行请先执行 python fetch_data.py）", W / 2, H / 2);
  ctx.textAlign = "left";
}

function drawLabel(node, s, isHover) {
  const px = s.w, py = s.h;
  const minDim = Math.min(px, py);
  // 极小方块：非悬停不画；悬停强制画（最小字号）
  if (minDim < 4 && !isHover) return;
  if (minDim < 4 && isHover) {
    ctx.font = "600 8px " + FONT_FAMILY;
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(fitText(node, 8, px), s.x + px / 2, s.y + py / 2);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    return;
  }
  const fontSize = autoFontSize(node.name, minDim, px - 5);
  ctx.font = "600 " + fontSize + "px " + FONT_FAMILY;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  if (node.leaf) {
    // 股票：名称 + 代码（+ 涨跌幅，格子够大时），按格子大小分三档
    const maxW = px - 5;
    let text = node.name;
    if (ctx.measureText(text).width > maxW) text = fitText(node, fontSize, maxW);
    if (ctx.measureText(text).width > maxW) text = text.slice(0, Math.max(1, Math.floor(maxW / (fontSize * 0.55))));
    const codeFont = Math.max(8, fontSize * 0.5);
    if (minDim > 44) {
      // 三行：名称 / 代码 / 涨跌幅
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, s.x + px / 2, s.y + py / 2 - fontSize * 0.78);
      ctx.font = "500 " + codeFont + "px " + FONT_FAMILY;
      ctx.fillStyle = "#8b95a7";
      if (node.code) ctx.fillText(node.code, s.x + px / 2, s.y + py / 2 + fontSize * 0.06);
      const rp = rangePctOf(node);
      if (rp !== null) {
        const sub = (rp > 0 ? "+" : "") + rp.toFixed(2) + "%";
        ctx.font = "500 " + Math.max(10, fontSize * 0.62) + "px " + FONT_FAMILY;
        ctx.fillStyle = pctTextColor(rp);
        ctx.fillText(sub, s.x + px / 2, s.y + py / 2 + fontSize * 0.78);
      }
    } else if (minDim > 26) {
      // 两行：名称 / 代码
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, s.x + px / 2, s.y + py / 2 - fontSize * 0.38);
      ctx.font = "500 " + codeFont + "px " + FONT_FAMILY;
      ctx.fillStyle = "#8b95a7";
      if (node.code) ctx.fillText(node.code, s.x + px / 2, s.y + py / 2 + fontSize * 0.2);
    } else {
      // 仅名称
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, s.x + px / 2, s.y + py / 2);
    }
  } else {
    // 行业：名称 + 涨跌幅
    const maxW = px - 6;
    const name = fitText(node, fontSize, maxW);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(name, s.x + px / 2, s.y + py / 2 - (minDim > 34 ? fontSize * 0.35 : 0));
    const irp = rangePctOf(node);
    if (minDim > 34 && irp !== null) {
      const sub = (irp > 0 ? "+" : "") + irp.toFixed(2) + "%";
      ctx.font = "500 " + Math.max(10, fontSize * 0.62) + "px " + FONT_FAMILY;
      ctx.fillStyle = pctTextColor(irp);
      ctx.fillText(sub, s.x + px / 2, s.y + py / 2 + fontSize * 0.55);
    }
  }
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
}

// 名称字号自适应：先按格子短边比例取基准字号，若完整名称放不下则按宽度压缩字号，
// 让短名称大字、长名称自动缩小，尽量显示全名（表格感）
function autoFontSize(name, minDim, maxW) {
  let fs = clamp(minDim * 0.42, 8, 34);
  ctx.font = "600 " + fs + "px " + FONT_FAMILY;
  let w = ctx.measureText(name).width;
  if (w > maxW && w > 0) {
    fs = Math.max(7, Math.floor(fs * maxW / w));
    ctx.font = "600 " + fs + "px " + FONT_FAMILY;
    w = ctx.measureText(name).width;
    if (w > maxW) fs = Math.max(6, Math.floor(fs * maxW / w));
  }
  return Math.max(6, fs);
}

function fitText(node, fontSize, maxW) {
  let text = node.name;
  ctx.font = "600 " + fontSize + "px " + FONT_FAMILY;
  if (ctx.measureText(text).width <= maxW) return text;
  while (text.length > 1 && ctx.measureText(text).width > maxW) {
    text = text.slice(0, -1);
  }
  return text;
}

// ---------- 命中检测 ----------
function nodeAtScreen(mx, my) {
  if (!layoutCache) return null;
  const wx = (mx - offsetX) / scale;
  const wy = (my - offsetY) / scale;
  let hit = null, bestArea = Infinity;
  for (const r of layoutCache) {
    if (wx >= r.x && wx < r.x + r.w && wy >= r.y && wy < r.y + r.h) {
      const a = r.w * r.h;
      if (a < bestArea) { bestArea = a; hit = r.node; }
    }
  }
  return hit;
}
const NOTE_BTN_MIN_LEN = 200;   // 格子长边（宽高较大者）超过此值才显示备注编辑按钮
const NOTE_BTN_H = 26, NOTE_BTN_PAD = 8;
function noteBtnRect(s) {
  const bw = Math.max(24, Math.min(30, s.w - 12));
  let x = s.x + s.w - bw - NOTE_BTN_PAD;
  if (x < s.x + 2) x = s.x + 2;
  return { x: x, y: s.y + NOTE_BTN_PAD, w: bw, h: NOTE_BTN_H };
}
function rrPath(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function hitNoteIcon(mx, my, node) {
  if (!node || !node.leaf || !node.code) return false;
  if (!current || current === root) return false;
  const r = rectOfNode(node);
  if (!r) return false;
  const s = worldToScreen(r);
  if (Math.max(s.w, s.h) < NOTE_BTN_MIN_LEN) return false;
  const b = noteBtnRect(s);
  return mx >= b.x - 3 && mx <= b.x + b.w + 3 && my >= b.y - 3 && my <= b.y + b.h + 3;
}

// ---------- 交互 ----------
let dragging = false, dragStartX = 0, dragStartY = 0, moved = 0;
let downPos = null, downTime = 0;
let lastTapName = null, lastTapTime = 0;   // 手动双击检测（行业格子：400ms 内同名两击 → 下钻）

cv.addEventListener("pointerdown", e => {
  dragging = true; moved = 0;
  downPos = { x: e.clientX, y: e.clientY };
  downTime = performance.now();
  cv.setPointerCapture(e.pointerId);
  cv.classList.add("dragging");
});

cv.addEventListener("pointermove", e => {
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  if (touchPinching) return;
  if (dragging && downPos) {
    const dx = e.clientX - downPos.x, dy = e.clientY - downPos.y;
    moved = Math.max(moved, Math.abs(dx) + Math.abs(dy));
    offsetX += dx; offsetY += dy;
    downPos = { x: e.clientX, y: e.clientY };
    draw();
    // v75：拖动浏览期间隐藏浮层（避免遮挡其他股票），结束保持隐藏；tap 由 pointerup 显示
    tooltip.style.display = "none";
    tooltipLocked = false;
    return;
  }
  if (tooltipLocked && !dragging) {
    // 鼠标在浮层上：冻结跟随，避免"追着鼠标跑"导致无法点击按钮
    const ttEl = document.getElementById("tooltip");
    if (!ttEl || ttEl.style.display === "none") tooltipLocked = false;
    else return;
  }
  const node = nodeAtScreen(mx, my);
  if (node !== hover) { hover = node; draw(); }
  updateTooltip(node, mx, my);
});

cv.addEventListener("pointerup", e => {
  dragging = false;
  cv.classList.remove("dragging");
  const dt = performance.now() - downTime;
  if (performance.now() < pinchEndAt) { downPos = null; return; }
  if (downPos && moved < 6 && dt < 400) {
    const rect = cv.getBoundingClientRect();
    const node = nodeAtScreen(e.clientX - rect.left, e.clientY - rect.top);
    if (node && node.leaf && hitNoteIcon(e.clientX - rect.left, e.clientY - rect.top, node)) {
      openNoteEditor(node.code, node.name, e.clientX, e.clientY);
    } else if (node && !node.leaf) {
      // v90 双击行业格子 → 直接下钻（400ms 内同名两击；手动检测，不依赖 dblclick 事件）
      const now = performance.now();
      if (lastTapName === node.name && (now - lastTapTime) < 400) {
        lastTapName = null; lastTapTime = 0;
        tooltip.style.display = "none"; tooltipLocked = false;
        drillInto(node);
      } else {
        lastTapName = node.name; lastTapTime = now;
        hover = node; draw(); updateTooltip(node, e.clientX - rect.left, e.clientY - rect.top, e.pointerType === "touch"); tooltipLocked = true; // v84 tap 行业显示并锁定浮层，点浮层内「下钻」按钮才进入成分股
      }
    }
    else if (node && node.leaf) {
      // v90 双击股票 → 跳雪球（与行业同款手动双击检测）
      const now2 = performance.now();
      if (lastTapName === node.code && (now2 - lastTapTime) < 400) {
        lastTapName = null; lastTapTime = 0;
        window.open("https://xueqiu.com/S/" + xueqiuSymbol(node.code), "_blank");
      } else {
        lastTapName = node.code; lastTapTime = now2;
        hover = node; draw(); updateTooltip(node, e.clientX - rect.left, e.clientY - rect.top, e.pointerType === "touch"); tooltipLocked = true; // v74 tap 股票显示并锁定浮层；v94 触屏居中显示
      }
    }
    else {
      // v90 双击空白 → 返回上级（原 dblclick 语义）
      const now3 = performance.now();
      const isDbl = lastTapName === "__blank__" && (now3 - lastTapTime) < 400;
      lastTapName = "__blank__"; lastTapTime = now3;
      tooltip.style.display = "none"; tooltipLocked = false; // v73 tap 空白隐藏 tooltip
      if (isDbl) goUp();
    }
  }
  downPos = null;
});

// ===== 自选股（watchlist，localStorage 本地存储） =====
const WL_KEY = "heatmap_watchlist";
let watchlist = [];
let wlPanelOpen = false;
const NOTES_KEY = "wl_notes_v1";
let wlNotes = {};
// ===== 成本价 v64: localStorage + 服务端 costs.json 双写 =====
const COSTS_KEY = "wl_costs_v1";
let wlCosts = {};
function costLoad() {
  try { wlCosts = JSON.parse(localStorage.getItem(COSTS_KEY)) || {}; } catch (e) { wlCosts = {}; }
  fetchCostsFile().catch(() => {});
}
async function fetchCostsFile() {
  const res = await fetch("/api/costs", { cache: "no-store" });
  if (!res.ok) return;
  const data = await res.json();
  if (!data || typeof data !== "object" || !data.costs || typeof data.costs !== "object") return;
  wlCosts = data.costs;
  try { localStorage.setItem(COSTS_KEY, JSON.stringify(wlCosts)); } catch (e) {}
  if (wlPanelOpen) renderWatchlist();
}
let _costSaveTimer = null;
function costSave() {
  try { localStorage.setItem(COSTS_KEY, JSON.stringify(wlCosts)); } catch (e) {}
  clearTimeout(_costSaveTimer);
  _costSaveTimer = setTimeout(() => {
    try {
      fetch("/api/costs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(wlCosts) }).catch(() => {});
    } catch (e) {}
  }, 500);
}
function costSet(code, cost, qty) {
  if (!code) return;
  if (cost == null || isNaN(cost) || cost <= 0) {
    delete wlCosts[code];
  } else {
    const item = { cost: Math.round(cost * 10000) / 10000 };
    if (qty != null && !isNaN(qty) && qty > 0) item.qty = Math.round(qty * 100) / 100;
    wlCosts[code] = item;
  }
  costSave();
  if (wlPanelOpen) renderWatchlist();
}
function costPnl(code, st) {
  const c = wlCosts ? wlCosts[code] : null;
  if (!c || !st || st.price == null || isNaN(st.price)) return null;
  const pnlPct = (st.price - c.cost) / c.cost * 100;
  return { cost: c.cost, qty: c.qty || null, pnlPct: pnlPct, pnlAmt: c.qty ? (st.price - c.cost) * c.qty : null };
}
function fmtQty(q) { return (q % 1 === 0) ? String(q) : q.toFixed(2); }
function wlNotesLoad() {
  try { wlNotes = JSON.parse(localStorage.getItem(NOTES_KEY)) || {}; } catch (e) { wlNotes = {}; }
  fetchNotesFile().catch(() => {});
}
async function fetchNotesFile() {
  const res = await fetch("/api/notes", { cache: "no-store" });
  if (!res.ok) return;
  const data = await res.json();
  if (!data || typeof data !== "object" || !data.notes || typeof data.notes !== "object") return;
  wlNotes = data.notes;
  try { localStorage.setItem(NOTES_KEY, JSON.stringify(wlNotes)); } catch (e) {}
  draw();
  if (lastTip && lastTip.node) updateTooltip(lastTip.node, lastTip.mx, lastTip.my, lastTip.centered);
  if (wlPanelOpen) renderWatchlist();
}
function wlNotesSave() {
  try { localStorage.setItem(NOTES_KEY, JSON.stringify(wlNotes)); } catch (e) {}
  try {
    fetch("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(wlNotes)
    }).catch(() => {});
  } catch (e) {}
}
function noteGet(code) { return (wlNotes && wlNotes[code]) ? wlNotes[code] : ""; }
const WL_EXPANDED_KEY = "wl_expanded_v1";
let wlExpanded = new Set();
try { const _saved = localStorage.getItem(WL_EXPANDED_KEY); if (_saved) wlExpanded = new Set(JSON.parse(_saved)); } catch (e) { wlExpanded = new Set(); }
function wlSaveExpanded() {
  try { localStorage.setItem(WL_EXPANDED_KEY, JSON.stringify([...wlExpanded])); } catch (e) {}
  wlSyncServer();
}
// 本机上次同步服务端的时刻（用于判断服务端发生的删除是否应生效）
const WL_LAST_SYNC_KEY = "ash_wl_last_sync";
let wlLastSyncMs = 0;
try { wlLastSyncMs = Number(localStorage.getItem(WL_LAST_SYNC_KEY)) || 0; } catch (e) { wlLastSyncMs = 0; }

function wlLoad() {
  try { watchlist = JSON.parse(localStorage.getItem(WL_KEY)) || []; }
  catch (e) { watchlist = []; }
  if (!Array.isArray(watchlist)) watchlist = [];
  wlSyncFromServer();
}
function wlSave() {
  try { localStorage.setItem(WL_KEY, JSON.stringify(watchlist)); } catch (e) {}
  wlSyncServer();
}
// 自选服务端备份（防抖 500ms 合并连续操作）
let wlSyncTimer = null;
function wlSyncServer() {
  clearTimeout(wlSyncTimer);
  wlSyncTimer = setTimeout(() => {
    fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ list: watchlist, collapsed: [...wlExpanded] })
    }).then(() => {
      // 推送成功即记为本机同步时刻（push 后本地与服务端一致）
      wlLastSyncMs = Date.now();
      try { localStorage.setItem(WL_LAST_SYNC_KEY, String(wlLastSyncMs)); } catch (e) {}
    }).catch(() => {});
  }, 500);
}
// 每次加载都从服务端拉取并合并（v81 跨端同步）
function wlSyncFromServer() {
  fetch("/api/watchlist", { cache: "no-store" })
    .then(r => r.json())
    .then(d => {
      if (!d || !d.ok || !Array.isArray(d.list)) return;
      const srvUpdatedMs = d.updated_ms || 0;
      // 服务端自上次本机同步后被改写，先按 ts 合并进本地
      if (srvUpdatedMs > wlLastSyncMs) {
        const merged = mergeWatchlists(watchlist, d.list, srvUpdatedMs);
        if (merged.changed) {
          watchlist = merged.list;
          try { localStorage.setItem(WL_KEY, JSON.stringify(watchlist)); } catch (e) {}
        }
        if (Array.isArray(d.collapsed) && d.collapsed.length) {
          wlExpanded = new Set(d.collapsed);
          try { localStorage.setItem(WL_EXPANDED_KEY, JSON.stringify([...wlExpanded])); } catch (e) {}
        }
        // 仅实际有变化才回写服务端，避免每次加载都重写文件形成回写循环
        if (merged.changed) wlSyncServer();
      }
      wlLastSyncMs = Math.max(wlLastSyncMs, srvUpdatedMs, Date.now());
      try { localStorage.setItem(WL_LAST_SYNC_KEY, String(wlLastSyncMs)); } catch (e) {}
      if (wlPanelOpen) renderWatchlist();
      draw();
    })
    .catch(() => {});
}
// 按 ts 合并两端自选：新增合并、删除保留（除非服务端在此后改写过）、ts 较新者胜
function mergeWatchlists(localList, serverList, srvUpdatedMs) {
  const out = [];
  const localMap = new Map();
  localList.forEach(it => { if (it && it.code) localMap.set(it.code, it); });
  serverList.forEach(it => {
    if (!it || !it.code) return;
    const l = localMap.get(it.code);
    if (l) {
      // 两边都有：取 ts 较新者；同名但 ts 相同取服务端
      out.push((Number(l.ts) || 0) > (Number(it.ts) || 0) ? l : it);
    } else {
      // 服务端有、本地无 → 新加入
      out.push(it);
    }
    localMap.delete(it.code);
  });
  // 本地有、服务端无：若服务端自上次本机同步后改写过，视为服务端删除了它
  localMap.forEach(l => {
    if (srvUpdatedMs > wlLastSyncMs) return; // 服务端较新 → 以服务端为准（删）
    out.push(l);
  });
  // changed 深比较：数量或任一条目 code/name/ts 不同都算变化
  const sig = list => JSON.stringify(list.map(it => [it.code, it.name, Number(it.ts) || 0]).sort((a, b) => a[0] < b[0] ? -1 : 1));
  return { list: out, changed: sig(out) !== sig(localList) };
}
function wlFind(code) { return watchlist.find(w => w.code === code); }
function wlAdd(node) {
  if (!node || !node.code) return false;
  if (wlFind(node.code)) return false;
  watchlist.push({ code: node.code, name: node.name, secid: (node.code.startsWith("6") ? "1." : "0.") + node.code, ts: Date.now() });
  wlSave(); renderWatchlist(); return true;
}
function wlRemove(code) {
  const i = watchlist.findIndex(w => w.code === code);
  if (i < 0) return false;
  watchlist.splice(i, 1);
  wlSave(); renderWatchlist(); return true;
}

// ===== v82 公告监控 =====
let monitored = new Set();
async function monitorLoad() {
  try {
    const res = await fetch("/api/monitor", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const j = await res.json();
    monitored = new Set((j && j.monitored || []).map(x => x.code));
    renderWatchlist();
  } catch (e) {
    monitored = new Set();
  }
}
function monitorSave() {
  const list = [];
  watchlist.forEach(w => {
    if (monitored.has(w.code)) list.push({ code: w.code, name: w.name, secid: w.secid || ("1." + w.code), ts: Date.now(), seen_ids: [] });
  });
  fetch("/api/monitor", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monitored: list })
  }).then(r => r.json()).then(j => {
    if (!(j && j.ok)) showToast("保存失败：" + ((j && j.msg) || "未知"));
  }).catch(() => showToast("服务不可用"));
}
async function monitorCfgLoad() {
  try {
    const res = await fetch("/api/monitorcfg", { cache: "no-store" });
    const j = await res.json();
    const c = (j && j.cfg) || {};
    const st = document.getElementById("mwhStatus");
    if (st) st.textContent = c.hasKey ? ("已配置 " + c.webhook) : "未配置";
    const inp = document.getElementById("mwhInput");
    if (inp) inp.placeholder = c.hasKey ? c.webhook : "企业微信群机器人 webhook 地址";
  } catch (e) {}
}
function mwhSave() {
  const inp = document.getElementById("mwhInput");
  const url = inp ? inp.value.trim() : "";
  if (!url) { showToast("请输入 webhook 地址"); return; }
  fetch("/api/monitorcfg", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ webhook: url })
  }).then(r => r.json()).then(j => {
    if (j && j.ok) { showToast("公告监控 webhook 已保存"); if (inp) inp.value = ""; monitorCfgLoad(); }
    else showToast("保存失败：" + ((j && j.msg) || "未知"));
  }).catch(() => showToast("服务不可用"));
}
async function mwhTestSend() {
  try {
    const res = await fetch("/api/monitortest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ msg: "来自公告监控面板的测试" }) });
    const j = await res.json();
    showToast(j && j.ok ? "测试消息已推送" : ("推送失败：" + ((j && j.msg) || "未知")));
  } catch (e) { showToast("测试失败：接口不可用"); }
}
const mwhSaveEl = document.getElementById("mwhSave");
if (mwhSaveEl) mwhSaveEl.addEventListener("click", mwhSave);
const mwhTestEl = document.getElementById("mwhTest");
if (mwhTestEl) mwhTestEl.addEventListener("click", mwhTestSend);

const MONITOR_MAX_COUNT = 20; // 与服务端 MONITOR_MAX_COUNT 一致
function toggleMonitor(code) {
  const w = wlFind(code);
  if (!w) return;
  if (monitored.has(code)) {
    monitored.delete(code);
    showToast("已取消监控：" + w.name);
  } else {
    if (monitored.size >= MONITOR_MAX_COUNT) {
      showToast("最多监控 " + MONITOR_MAX_COUNT + " 只（数据窗口限制），请先移除部分");
      return;
    }
    monitored.add(code);
    showToast("已开启监控：" + w.name + "，新公告将推送到微信群");
  }
  monitorSave();
  renderWatchlist();
}
function wlClear() { watchlist = []; wlSave(); renderWatchlist(); }
// toast 轻提示（2 秒自动消失）
let toastTimer = null;
function showToast(msg) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2000);
}
// 右键切换自选：统一入口（与浮层按钮共用 wlAdd/wlRemove，状态一致）
function toggleWatchlist(node) {
  if (!node || !node.code) return;
  const inWl = wlFind(node.code);
  if (inWl) {
    wlRemove(node.code);
    showToast("已移出自选：" + node.name + " " + node.code);
  } else {
    wlAdd(node);
    showToast("已加入自选：" + node.name + " " + node.code);
  }
  // 浮层按钮同步（若正显示该股）
  const b = document.getElementById("ttWlBtn");
  if (b && b.getAttribute("data-code") === node.code) {
    b.textContent = inWl ? "☆ 加自选" : "★ 移出自选";
    b.classList.toggle("on", !inWl);
  }
  // 格子金边闪烁反馈
  flashNode = node;
  flashUntil = performance.now() + 800;
  draw();
  setTimeout(() => { if (flashNode === node) { flashNode = null; draw(); } }, 800);
}
function wlPanelPos() {
  // 自选面板 fixed 定位：顶部避开 header+crumb（桌面端；移动端全屏由 CSS 处理）
  const panel = document.getElementById("wlPanel");
  if (!panel) return;
  if (window.matchMedia && window.matchMedia("(max-width: 768px)").matches) return;
  const h = (document.querySelector("header") ? document.querySelector("header").offsetHeight : 0);
  const c = (document.getElementById("crumb") ? document.getElementById("crumb").offsetHeight : 0);
  panel.style.top = (h + c + 12) + "px";
  panel.style.maxHeight = "calc(100% - " + (h + c + 24) + "px)";
}
function wlToggle() {
  wlPanelOpen = !wlPanelOpen;
  const panel = document.getElementById("wlPanel");
  if (panel) panel.classList.toggle("hidden", !wlPanelOpen);
  if (wlPanelOpen) { wlPanelPos(); renderWatchlist(); monitorCfgLoad(); }
}
window.addEventListener("resize", () => { if (wlPanelOpen) wlPanelPos(); });
function wlRowHtml(w) {
  const st = codeIndex ? codeIndex.get(w.code) : null;
  const cp = costPnl(w.code, st);
  let costHtml;
  if (cp) {
    costHtml = '<div class="wl-cost" data-code="' + w.code + '"><span class="wl-costbtn" title="设置成本价">成</span><span>成本 ' + fmtNum(cp.cost) + '</span><span style="color:' + pctTextColor(cp.pnlPct) + ';font-weight:600">' + (cp.pnlPct > 0 ? "+" : "") + cp.pnlPct.toFixed(2) + "%</span></div>";
  } else {
    costHtml = '<div class="wl-cost" data-code="' + w.code + '"><span class="wl-costbtn" title="设置成本价">成</span><span class="wl-costempty">设置成本价</span></div>';
  }
  const rp = st ? rangePctOf(st) : null;
  const rl = RANGES.find(r => r.id === currentRange).label;
  const priceStr = st && st.price != null && !isNaN(st.price) ? fmtNum(st.price) : "—";
  const rpStr = rp === null || rp === undefined || isNaN(rp) ? "—" : ((rp > 0 ? "+" : "") + rp.toFixed(2) + "%");
  const pcStr = st && st.pct != null && !isNaN(st.pct) ? ((st.pct > 0 ? "+" : "") + st.pct.toFixed(2) + "%") : "—";
  return '<div class="wl-row' + (cp ? " has-cost" : "") + '" data-code="' + w.code + '" title="点击跳雪球">' +
    '<div class="wl-main"><span class="wl-name">' + w.name + '</span><span class="wl-code">' + w.code + "</span></div>" +
    '<div class="wl-nums"><span class="wl-price">' + priceStr + "</span>" +
    '<span class="wl-rp" style="color:' + pctTextColor(rp) + '">' + rpStr + " <i>" + rl + "</i></span>" +
    (currentRange !== "day" ? '<span class="wl-day" style="color:' + pctTextColor(st ? st.pct : null) + '">当日 ' + pcStr + "</span>" : "") +
    costHtml + "</div>" +
    '<button class="wl-mon' + (monitored.has(w.code) ? " on" : "") + '" data-code="' + w.code + '" title="监控公告">' + (monitored.has(w.code) ? "停" : "监") + "</button>" +
    '<button class="wl-del" data-code="' + w.code + '" title="移出自选">×</button></div>';
}
function renderWatchlist() {
  const cnt = document.getElementById("wlCount");
  if (cnt) cnt.textContent = watchlist.length;
  const btn = document.getElementById("btnWl");
  if (btn) btn.classList.toggle("has", watchlist.length > 0);
  const panel = document.getElementById("wlPanel");
  const list = document.getElementById("wlList");
  if (!panel || !list) return;
  const sub = document.getElementById("wlSub");
  if (sub) sub.textContent = wlPanelOpen ? (isTradingTime() ? "实时" : "收盘快照") : "";
  if (watchlist.length === 0) {
    hideWlTip();
    list.innerHTML = '<div class="wl-empty">还没有自选股<br>去个股页悬停格子点 ☆ 添加</div>';
    return;
  }
  const esc = s0 => String(s0).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  // v71 方案B：有成本价的股票移入置顶「持仓」分类，行业分组只留无成本价的
  const holdings = watchlist.filter(w => wlCosts && wlCosts[w.code]);
  const rest = watchlist.filter(w => !(wlCosts && wlCosts[w.code]));
  let wlHtml = "";
  if (holdings.length > 0) {
    const collapsed = !wlExpanded.has("__holdings__");
    wlHtml += '<div class="wl-group wl-holding' + (collapsed ? " collapsed" : "") + '" data-ind="__holdings__">' +
      '<div class="wl-ghead" title="设置过成本价的股票"><span class="wl-garrow"></span><span class="wl-gname">持仓</span>' +
      '<span class="wl-gcnt">' + holdings.length + '</span></div>' +
      '<div class="wl-gbody">' + holdings.map(wlRowHtml).join("") + '</div></div>';
  }
  const groups = [];
  for (const w of rest) {
    const st = codeIndex ? codeIndex.get(w.code) : null;
    const ind = (st && st.industry) ? st.industry : "未分类";
    let g = groups.find(x => x.name === ind);
    if (!g) { g = { name: ind, items: [] }; groups.push(g); }
    g.items.push(w);
  }
  wlHtml += groups.map(g => {
    const collapsed = !wlExpanded.has(g.name);
    return '<div class="wl-group' + (collapsed ? " collapsed" : "") + '" data-ind="' + esc(g.name) + '">' +
      '<div class="wl-ghead"><span class="wl-garrow"></span><span class="wl-gname">' + esc(g.name) + '</span>' +
      '<span class="wl-gcnt">' + g.items.length + '</span></div>' +
      '<div class="wl-gbody">' + g.items.map(wlRowHtml).join("") + '</div></div>';
  }).join("");
  list.innerHTML = wlHtml;
  if (wlTipCode) {
    const row = list.querySelector('.wl-row[data-code="' + wlTipCode + '"]');
    if (row) showWlTip(row); else hideWlTip();
  }
}
// 自选股行情：8 秒实时并入（独立一次 ulist 请求，量小无压力）
async function fetchWatchlistQuotes() {
  const secids = watchlist.map(w => w.secid);
  if (secids.length === 0) return;
  try {
    const res = await fetch(QUOTE_URL + "?secids=" + secids.join(",") + "&fields=f12,f2,f3,f4,f109,f160,f110,f24,f25&fltt=2&invt=2&np=1");
    const j = await res.json();
    const arr = (j && j.data && j.data.diff) ? j.data.diff : null;
    if (!arr) return;
    const map = {};
    for (const x of arr) map[String(x.f12)] = x;
    let changed = false;
    for (const w of watchlist) {
      const x = map[w.code];
      const st = codeIndex ? codeIndex.get(w.code) : null;
      if (!x || !st) continue;
      if (x.f2 !== null && x.f2 !== undefined && x.f2 !== "-") st.price = Number(x.f2);
      if (x.f3 !== null && x.f3 !== undefined && x.f3 !== "-") st.pct = Number(x.f3);
      if (x.f4 !== null && x.f4 !== undefined && x.f4 !== "-") st.chg = Number(x.f4);
      if (x.f109 !== null && x.f109 !== undefined && x.f109 !== "-") st.pct5 = Number(x.f109);
      if (x.f160 !== null && x.f160 !== undefined && x.f160 !== "-") st.pct10 = Number(x.f160);
      if (x.f110 !== null && x.f110 !== undefined && x.f110 !== "-") st.pct20 = Number(x.f110);
      if (x.f24 !== null && x.f24 !== undefined && x.f24 !== "-") st.pct60 = Number(x.f24);
      if (x.f25 !== null && x.f25 !== undefined && x.f25 !== "-") st.pctYTD = Number(x.f25);
      changed = true;
    }
    if (changed && wlPanelOpen) renderWatchlist();
  } catch (e) { /* 静默失败，下轮重试 */ }
}

// 雪球代码：根据 A 股代码生成带市场前缀的雪球 symbol（如 601688 → SH601688）
function xueqiuSymbol(code) {
  if (!code) return code;
  const c = String(code).replace(/[^0-9]/g, "");
  if (/^(00|20|30)/.test(c)) return "SZ" + c;   // 深主板/深B/创业板
  if (/^(43|83|87|88|92)/.test(c)) return "BJ" + c; // 北交所
  return "SH" + c;                                // 沪主板/科创板（60/68/9）及兜底
}

// v90 双击语义统一走 pointerup tap 手动双击检测（dblclick 事件在指针捕获下不可靠，已移除）

cv.addEventListener("wheel", e => {
  e.preventDefault();
  const rect = cv.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const k = Math.exp(-e.deltaY * 0.0016);
  const ns = clamp(scale * k, baseScale * 0.5, MAX_SCALE);
  const wx = (mx - offsetX) / scale;
  const wy = (my - offsetY) / scale;
  offsetX = mx - wx * ns;
  offsetY = my - wy * ns;
  scale = ns;
  draw();
}, { passive: false });

window.addEventListener("resize", () => {
  const oldW = W, oldH = H;
  sizeCanvas();
  // v91：WORLD_W 变化（横竖切换）→ baseScale/视图重算；否则保持平移增量
  const baseWas = baseScale;
  baseScale = (WORLD_W < 1 ? H : Math.min(W, H)) * 0.98;
  if (WORLD_W !== (oldH > oldW * 1.05 ? oldW / oldH : 1)) {
    // 横竖切换：直接重置视图（比例变化太大，增量无意义）
    resetView();
    relayout();
  } else {
    baseScale = (baseWas > 0) ? baseScale : baseWas;
    offsetX += (W - oldW) / 2;
    offsetY += (H - oldH) / 2;
  }
  draw();
});

// ---------- Tooltip ----------
// v92 悬停 F10 财务异步填充：/api/f10?code=（server 端按需缓存），同 code 复用
// v93 修复竞态：① 同 code 在途去重（mousemove 每次都触发，不再打出重复请求）；
//     ② 失败（{ok:false}/网络错误）不把「—」固化，保留「…」，浮层仍指向该股时自动重试（最多 2 次）
let _f10Cache = {};
let _f10Pending = {};   // code -> 在途 Promise（去重）
let _f10Retry = {};     // code -> 连续失败次数（成功后清零）
let _f10ReqSeq = 0;
function fetchF10ForTip(code) {
  if (!code) return;
  const seq = ++_f10ReqSeq;
  const render = d => {
    if (seq !== _f10ReqSeq) return;   // 只渲染最新一次悬停
    const dnp = (d && d.deductNp != null) ? fmtYi(d.deductNp) : "—";
    const dny = (d && d.deductNpYoy != null) ? ((d.deductNpYoy > 0 ? "+" : "") + d.deductNpYoy.toFixed(2) + "%") : "—";
    const oc = (d && d.opCash != null) ? fmtYi(d.opCash) : "—";
    const sr = (d && d.speedRatio != null) ? d.speedRatio.toFixed(2) : "—";
    const rd = (d && d.reportDate) ? d.reportDate : "—";
    const dnyColor = (d && d.deductNpYoy != null) ? pctTextColor(d.deductNpYoy) : "#8b95a7";
    const el0 = document.querySelector('.tt-f10row0[data-code="' + code + '"] .tt-f10v0');
    const el1 = document.querySelector('.tt-f10row[data-code="' + code + '"] .tt-f10v');
    const el2 = document.querySelector('.tt-f10row2[data-code="' + code + '"] .tt-f10v2');
    const el3 = document.querySelector('.tt-f10row3[data-code="' + code + '"] .tt-f10v3');
    const el4 = document.querySelector('.tt-f10row4[data-code="' + code + '"] .tt-f10v4');
    if (el0) el0.textContent = rd;
    if (el1) el1.textContent = dnp;
    if (el2) { el2.textContent = dny; el2.style.color = dnyColor; }
    if (el3) el3.textContent = oc;
    if (el4) el4.textContent = sr;
  };
  if (_f10Cache[code]) { render(_f10Cache[code]); return; }
  if (!_f10Pending[code]) {
    _f10Pending[code] = fetch("/api/f10?code=" + code).then(r => r.json()).then(j => {
      if (!j || !j.ok || !j.data) return null;   // 竞态/网络失败 → null，走重试
      if (!j.data.error) { _f10Cache[code] = j.data; _f10Retry[code] = 0; }
      return j.data;   // 带 error 的也返回：服务端确认无数据 → 渲染「—」（正确终态）
    }).catch(() => null).finally(() => { delete _f10Pending[code]; });
  }
  _f10Pending[code].then(d => {
    if (d) { render(d); return; }
    if ((_f10Retry[code] || 0) >= 2) return;
    _f10Retry[code] = (_f10Retry[code] || 0) + 1;
    setTimeout(() => {
      if (lastTip && lastTip.node && lastTip.node.code === code) fetchF10ForTip(code);
    }, 800);
  });
}

function updateTooltip(node, mx, my, centered) {
  if (!node) { tooltip.style.display = "none"; tooltipLocked = false; return; }
  const ind = node.leaf ? node.parent : null;
  const esc = s0 => String(s0).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  let html = '<div class="tt-name">' + node.name;
  if (node.code) html += '<span class="tt-code">' + node.code + "</span>";
  html += "</div>";
  html += '<span class="tt-close" title="关闭">×</span>';
  if (node.leaf) {
    const price = node.price;
    // v94 涨跌幅不再入悬浮窗：格子上画的 rangePctOf(node) 与之完全相同，纯重复
    html += '<div class="tt-row"><span class="tt-label">现价</span><span>' + fmtNum(price) + "</span></div>";
    const fmtYi2 = v => (v === null || v === undefined || isNaN(v)) ? "—" : fmtYi(v);
    const fs1 = node.peTTM;
    html += '<div class="tt-row tt-f10row0" data-code="' + node.code + '"><span class="tt-label">报告期</span><span class="tt-f10v0">…</span></div>';
    html += '<div class="tt-row"><span class="tt-label">市盈率TTM</span><span>' + (fs1 === null || fs1 === undefined || isNaN(fs1) ? "—" : fs1.toFixed(2)) + "</span></div>";
    html += '<div class="tt-row"><span class="tt-label">总营收</span><span>' + fmtYi2(node.rev) + "</span></div>";
    const ry = node.revYoy;
    html += '<div class="tt-row"><span class="tt-label">总营收同比</span><span style="color:' + pctTextColor(ry) + '">' + (ry === null || ry === undefined || isNaN(ry) ? "—" : ((ry > 0 ? "+" : "") + ry.toFixed(2) + "%")) + "</span></div>";
    html += '<div class="tt-row"><span class="tt-label">归母净利润</span><span>' + fmtYi2(node.np) + "</span></div>";
    const ny = node.npYoy;
    html += '<div class="tt-row"><span class="tt-label">归母净利润同比</span><span style="color:' + pctTextColor(ny) + '">' + (ny === null || ny === undefined || isNaN(ny) ? "—" : ((ny > 0 ? "+" : "") + ny.toFixed(2) + "%")) + "</span></div>";
    html += '<div class="tt-row tt-f10row" data-code="' + node.code + '"><span class="tt-label">扣非净利润</span><span class="tt-f10v">…</span></div>';
    html += '<div class="tt-row tt-f10row2" data-code="' + node.code + '"><span class="tt-label">扣非同比</span><span class="tt-f10v2">…</span></div>';
    html += '<div class="tt-row tt-f10row3" data-code="' + node.code + '"><span class="tt-label">经营现金流</span><span class="tt-f10v3">…</span></div>';
    html += '<div class="tt-row tt-f10row4" data-code="' + node.code + '"><span class="tt-label">速动比率</span><span class="tt-f10v4">…</span></div>';
    if (ind) html += '<div class="tt-ind">行业：' + ind.name + "</div>";
    const note = noteGet(node.code);
    if (note) {
      html += '<div class="tt-note has" data-code="' + node.code + '"><div class="tt-note-text">' + esc(note) + '</div><div class="tt-note-ops"><span class="tt-note-edit">编辑</span><span class="tt-note-del">删除</span></div></div>';
    } else {
      html += '<div class="tt-note" data-code="' + node.code + '"><span class="tt-note-add">＋ 备注</span></div>';
    }
    const inWl = wlFind(node.code);
    html += '<div class="tt-row tt-wlrow">'
      + '<button id="ttWlBtn" data-code="' + node.code + '" class="wl-btn' + (inWl ? " on" : "") + '">' + (inWl ? "★ 移出自选" : "☆ 加自选") + '</button>'
      + '<button id="ttAlertBtn" data-code="' + node.code + '" class="wl-btn" title="设置价格/涨跌幅告警，触发推送到企业微信群">🔔 告警</button>'
      + '<a id="ttXqBtn" class="wl-btn wl-link-btn" href="https://xueqiu.com/S/' + xueqiuSymbol(node.code) + '" target="_blank" rel="noopener">雪球详情 ↗</a>'
      + '</div>';
  } else {
    const pc = node.pct;
    const rp = rangePctOf(node);
    const rl = RANGES.find(r => r.id === currentRange).label;
    const rpStr = rp === null ? "—" : ((rp > 0 ? "+" : "") + rp.toFixed(2) + "%");
    const pctStr = pc === null || pc === undefined || isNaN(pc) ? "—" : ((pc > 0 ? "+" : "") + pc.toFixed(2) + "%");
    html += '<div class="tt-row"><span class="tt-label">' + rl + '涨跌幅</span><span style="color:' + pctTextColor(rp) + ';font-weight:600">' + rpStr + "</span></div>";
    if (currentRange !== "day") html += '<div class="tt-row"><span class="tt-label">当日涨跌幅</span><span style="color:' + pctTextColor(pc) + '">' + pctStr + "</span></div>";
    html += '<div class="tt-row"><span class="tt-label">成分股</span><span>' + node.children.length + " 只</span></div>";
    html += '<div class="tt-row"><span class="tt-label">总市值</span><span>' + fmtYi(node.value) + "</span></div>";
    if (node.pe != null && !isNaN(node.pe)) html += '<div class="tt-row"><span class="tt-label">平均市盈率</span><span>' + fmtNum(node.pe) + '</span></div>';
    html += '<div class="tt-row tt-wlrow"><button id="ttDrillBtn" data-name="' + node.name + '" class="wl-btn" title="进入该行业的成分股页面">↓ 下钻查看成分股</button></div>';
  }
  tooltip.innerHTML = html;
  // v93.1 F10 填充必须在 innerHTML 赋值之后：缓存命中时 render 同步执行，
  // 若在赋值前调用会填充旧 DOM 再被整体重写覆盖，导致第二次悬停起永远显示「…」
  fetchF10ForTip(node.code);
  tooltip.style.display = "block";
  const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
  let tx, ty;
  if (centered) {
    // v94 移动端 tap：视口居中显示——tap 点即手指位置，锚点定位会被手指遮挡，小屏上翻转后还会越界
    tx = (W - tw) / 2;
    ty = (H - th) / 2;
  } else {
    tx = mx + 14; ty = my + 14;
    if (tx + tw > W) tx = mx - tw - 12;
    if (ty + th > H) ty = my - th - 12;
  }
  // 边界钳制：翻转后坐标可能为负（浮层顶部被裁出屏外），统一下限 8px；上限防超宽屏外溢
  tx = Math.max(8, Math.min(tx, W - tw - 8));
  ty = Math.max(8, Math.min(ty, H - th - 8));
  tooltip.style.left = tx + "px";
  tooltip.style.top = ty + "px";
  lastTip = { node: node, mx: mx, my: my, centered: !!centered };
}

// ---------- 搜索 ----------
let searchTimer = null;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchEl.value.trim();
  if (!q) { dropdownEl.style.display = "none"; return; }
  searchTimer = setTimeout(() => showDropdown(q), 150);
});

function showDropdown(q) {
  if (!root) return;
  const up = q.toUpperCase();
  const hits = [];
  // 行业匹配（v90 三层）：二级行业为主，一级行业兜底（低分）
  for (const sec of root.children) {
    for (const ind of sec.children) {
      if (ind.name.indexOf(q) >= 0) {
        hits.push({ type: "ind", ind: ind, score: ind.name === q ? 0 : 1 });
      }
    }
  }
  for (const sec of root.children) {
    if (sec.name.indexOf(q) >= 0) hits.push({ type: "ind", ind: sec, score: 2 });
  }
  // 股票匹配（三层遍历到个股）
  outer2:
  for (const sec of root.children) {
    for (const ind of sec.children) {
      for (const st of ind.children) {
        const matchCode = st.code.indexOf(up) === 0;
        const matchCodeIn = !matchCode && st.code.indexOf(up) >= 0;
        const matchName = st.name.indexOf(q) >= 0;
        if (matchCode || matchName || matchCodeIn) {
          hits.push({ type: "st", st: st, ind: ind.name, score: (matchCode ? 2 : matchName ? 3 : 4) });
          if (hits.length >= 60) break outer2;
        }
      }
    }
  }
  hits.sort((a, b) => a.score - b.score);
  const top = hits.slice(0, 12);
  if (top.length === 0) { dropdownEl.style.display = "none"; return; }
  let html = "";
  for (const h of top) {
    if (h.type === "ind") {
      const pc = h.ind.pct;
      const pctStr = pc === null || pc === undefined || isNaN(pc) ? "—" : ((pc > 0 ? "+" : "") + pc.toFixed(2) + "%");
      html += '<div class="item" data-sector="' + h.ind.name + '"><span>' + h.ind.name +
        '<span class="d-tag">行业</span></span>' +
        '<span class="d-pct" style="color:' + pctTextColor(pc) + '">' + pctStr + "</span></div>";
    } else {
      const pc = h.st.pct;
      const pctStr = pc === null || pc === undefined || isNaN(pc) ? "—" : ((pc > 0 ? "+" : "") + pc.toFixed(2) + "%");
      html += '<div class="item" data-code="' + h.st.code + '"><span>' + h.st.name +
        '<span class="d-code"> ' + h.st.code + " · " + h.ind + "</span></span>" +
        '<span class="d-pct" style="color:' + pctTextColor(pc) + '">' + pctStr + "</span></div>";
    }
  }
  dropdownEl.innerHTML = html;
  dropdownEl.style.display = "block";
  dropdownEl.querySelectorAll(".item").forEach(el => {
    el.addEventListener("click", () => {
      if (el.dataset.sector) {
        goToSector(el.dataset.sector);
      } else {
        goToStock(el.dataset.code);
      }
      dropdownEl.style.display = "none";
      searchEl.value = "";
    });
  });
}

document.addEventListener("click", e => {
  if (!dropdownEl.contains(e.target) && e.target !== searchEl) dropdownEl.style.display = "none";
});

function goToStock(code) {
  if (!root) return;
  // 找股票所在行业
  let indNode = null, stockNode = null;
  outer:
  for (const sec of root.children) {
    for (const ind of sec.children) {
      for (const st of ind.children) {
        if (st.code === code) { indNode = ind; stockNode = st; break outer; }
      }
    }
  }
  if (!stockNode) return;
  // 下钻到行业
  if (current !== indNode) {
    current = indNode;
    relayout();
  }
  const r = rectOfNode(stockNode);
  if (r) zoomToNode(r, 0.55);
  highlight = { code: code, t0: performance.now() };
  renderCrumb();
  draw();
}

function findNodeByCode(code) {
  if (!current || !current.children) return null;
  for (const c of current.children) if (c.code === code) return c;
  return null;
}

// v70：定位行业块（回根层 + 缩放 + 高亮闪烁）
function goToSector(name) {
  if (!root) return;
  if (current !== root) {
    current = root;
    relayout();
  }
  let indNode = root.children.find(c => c.name === name) || null;
  if (!indNode) {
    for (const sec of root.children) {
      const hit = sec.children.find(c => c.name === name);
      if (hit) { indNode = hit; break; }
    }
  }
  if (!indNode) return;
  let r = rectOfNode(indNode);
  if (!r) {
    // 一级组无独立 rect：取本组二级成员的并集矩形
    let x1 = 1e18, y1 = 1e18, x2 = -1e18, y2 = -1e18, any = false;
    for (const rc of layoutCache) {
      const p = rc.node ? rc.node.parent : null;
      if (p === indNode) {
        any = true; x1 = Math.min(x1, rc.x); y1 = Math.min(y1, rc.y);
        x2 = Math.max(x2, rc.x + rc.w); y2 = Math.max(y2, rc.y + rc.h);
      }
    }
    if (any) r = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  }
  if (r) zoomToNode(r, 0.55);
  highlight = { name: name, t0: performance.now() };
  renderCrumb();
  draw();
}

function findNodeByName(name) {
  if (!current || !current.children) return null;
  for (const c of current.children) if (c.name === name) return c;
  return null;
}

// v70 快捷键：/ 聚焦搜索（输入框内不劫持）、Esc 失焦
document.addEventListener("keydown", e => {
  const tag = document.activeElement && document.activeElement.tagName;
  if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA" && !(document.activeElement && document.activeElement.isContentEditable)) {
    e.preventDefault();
    searchEl.focus();
    searchEl.select();
  } else if (e.key === "Escape" && document.activeElement === searchEl) {
    searchEl.blur();
    dropdownEl.style.display = "none";
  }
});

// ---------- 数据加载 ----------
async function fetchData(showStatus) {
  // 移动端/Tailscale 场景：首启可能因隧道重建瞬时失败，自动重试一次（无感知）
  try {
    try {
      await fetchOnce(showStatus);
      return;
    } catch (e0) {
      console.warn("fetchData 首次失败，1.5s 后重试:", e0 && e0.message);
      await new Promise(r => setTimeout(r, 1500));
    }
  } catch (eOuter) {}
  await fetchOnce(showStatus);
}

// v98：data.json 共享加载器——并发请求合并为一次下载+解析（首页 init 的
// fetchOnce/fetchSpot/fetchRe 三连发只需要同一份快照的不同字段，各拉一遍浪费 2/3 流量）
let _djLast = null, _djLastAt = 0, _djInflight = null;
let _djVer = null;   // v99：当前持有快照的版本（data.json mtime 秒级，来自响应 Last-Modified）
function loadDataJson() {
  if (_djInflight) return _djInflight;
  _djInflight = fetch("data.json?t=" + Date.now(), { cache: "no-store" })
    .then(res => res.json().then(payload => {
      const lm = res.headers.get("Last-Modified");   // 秒级 mtime，与 /api/version 同源可比
      _djVer = lm ? Math.floor(Date.parse(lm) / 1000) : null;
      _djLast = payload; _djLastAt = Date.now();
      return payload;
    }))
    .finally(() => { _djInflight = null; });
  return _djInflight;
}
// v99：快照版本探测（~300B）。失败返回 null，调用方按"变了"处理（fail-open）
async function probeDataVer() {
  try {
    const res = await fetch("/api/version?t=" + Date.now(), { cache: "no-store" });
    const j = await res.json();
    return (j && j.ok && j.v) ? j.v : null;
  } catch (e) {
    return null;
  }
}
// v99：自动刷新轮询的节流——快照没变（午休/收盘/周末服务端不写盘）就不拉全包。
// 只挂 60s 定时器路径；手动「全量更新」不探测，保证即时回显。
async function autoFetchTick() {
  const v = await probeDataVer();
  if (v !== null && _djVer === v) return;
  await fetchData(false);
}
// 快照是否够新：自动刷新周期 60s，90s 内视为有效；过期则重新拉，
// 避免关闭自动刷新长时间挂页后，现货/稀土模块一直读极旧的快照
function freshPayload() {
  return (_djLast && Date.now() - _djLastAt < 90000) ? _djLast : null;
}

async function fetchOnce(showStatus) {
  try {
    const payload = await loadDataJson();
    const oldCurrentName = current ? current.name : null;
    const oldIdx = codeIndex; // 旧树索引（可能含实时价格）
    root = buildTree(payload);
    // 合并实时价格：60 秒结构刷新不丢失实时行情（避免盘中价格跳回快照值）
    // v96：仅当旧值确实更新（本页实时循环最后成功晚于快照落盘时刻）才合并；快照更新时以快照为准——
    // 否则午休/收盘后打开的页面会被页面内旧价持续盖住服务端新快照（含手动"全量更新"成功后的回显）
    const snapAt = Date.parse(String(payload.fetched_at || "").replace(" ", "T"));
    if (oldIdx && oldIdx.size > 0 && (!(snapAt > 0) || lastLiveAt > snapAt)) {
      // v90 三层树：合并到个股层（第三层），再自下而上重算 二级→一级→root
      for (const sec of root.children) {
        for (const ind of sec.children) {
          for (const st of ind.children) {
            const o = oldIdx.get(st.code);
            if (o) {
              st.price = o.price; st.pct = o.pct; st.chg = o.chg; st.amount = o.amount;
              st.pct5 = o.pct5; st.pct10 = o.pct10; st.pct20 = o.pct20; st.pct60 = o.pct60; st.pctYTD = o.pctYTD;
            }
          }
          recalcNodePcts(ind, ind.children);
        }
        recalcNodePcts(sec, sec.children.reduce((x, ind) => x.concat(ind.children), []));
      }
      recalcNodePcts(root, root.children.reduce((x, sec) => x.concat(sec.children.reduce((y, ind) => y.concat(ind.children), [])), []));
    }
    buildCodeIndex();
    if (wlPanelOpen) renderWatchlist();
    fetchedAt = payload.fetched_at || "";
    totalStocks = payload.total_stocks || 0;
    totalIndustries = payload.total_industries || 0;
    // 保留当前下钻视图（按名字匹配），否则回根
    if (oldCurrentName && oldCurrentName !== "全部A股") {
      let found = null;
      for (const sec of root.children) {
        if (sec.name === oldCurrentName) { found = sec; break; }
        for (const ind of sec.children) if (ind.name === oldCurrentName) { found = ind; break; }
        if (found) break;
      }
      current = found || root;
    } else {
      current = root;
    }
    // URL ?range=xxx 自动切换区间（仅首次加载应用，如 ?range=d20）
    if (!rangeAppliedFromUrl) {
      const urlRange = new URLSearchParams(location.search).get("range");
      if (urlRange && RANGES.some(r => r.id === urlRange)) {
        currentRange = urlRange;
        const rSel = document.getElementById("rangeSel");
        if (rSel) rSel.value = urlRange;
        rangeAppliedFromUrl = true;
      }
    }
    relayout();
    renderCrumb();
    draw();
    updateStats();
    fetchIndices();
    liveFailures = 0;
    if (showStatus) flashRefresh("已更新");
  } catch (err) {
    console.error(err);
    if (showStatus) flashRefresh("加载失败");
  }
}

// ---------- 指数（页面上方：上证/创业板/科创50/沪深300） ----------
const INDEX_SECIDS = "1.000001,0.399006,1.000688,1.000300,0.399001"; // 末位深证成指：仅用于计算两市成交额（f6），不渲染

function fetchIndices() {
  const cbName = "idxCb" + Date.now() + Math.floor(Math.random() * 1000);
  const url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get?secids=" + INDEX_SECIDS +
    "&fields=f2,f3,f4,f5,f6,f12,f14&fltt=2&invt=2&np=1&cb=" + cbName;
  const s = document.createElement("script");
  s.src = url;
  window[cbName] = function (json) {
    renderIndices(json);
    delete window[cbName];
    s.remove();
  };
  s.onerror = function () { delete window[cbName]; s.remove(); };
  document.head.appendChild(s);
}

// ---------- 实时行情（8 秒刷新，直连东财 push2delay，与 52etf 同源） ----------
let liveTimer = null;
let liveFailures = 0;
let liveMode = false;
let codeIndex = null;
let lastLiveAttempt = 0;
let lastLiveAt = 0;   // v96：主实时循环最近一次成功时刻（ms）；0=本页尚未跑过实时。fetchOnce 合并新旧价格的判据

function isTradingTime() {
  const now = new Date();
  const d = now.getDay();
  if (d === 0 || d === 6) return false;
  const t = now.getHours() * 60 + now.getMinutes();
  return (t >= 9 * 60 + 30 && t <= 11 * 60 + 30) || (t >= 13 * 60 && t <= 15 * 60);
}

function buildCodeIndex() {
  codeIndex = new Map();
  if (!root) return;
  for (const sec of root.children) {
    for (const ind of sec.children) {
      for (const st of ind.children) codeIndex.set(st.code, st);
    }
  }
}

async function fetchQuotesBatch(secids) {
  const qs = "secids=" + secids.join(",") +
    "&fields=f12,f2,f3,f4,f6,f109,f160,f110,f24,f25&fltt=2&invt=2&np=1&pn=1&pz=" + secids.length;
  const res = await fetch(QUOTE_URL + "?" + qs, { cache: "no-store" });
  if (!res.ok) throw new Error("quote http " + res.status);
  const j = await res.json();
  return (j && j.data && j.data.diff) || [];
}

// ---------- 全球指数地图（v20） ----------
function gNorm(v) {
  if (v === null || v === undefined || v === "-" || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
// v99 交易所交易时段表（按指数代码）：[时区, 当地周几(0=周日), 时段[start,end]...（当地时间）]
// 状态改为时刻表驱动（能表达 午休/未开盘）；东财源叠加行情时间戳兜底：时刻表说开盘中但 45 分钟无新打印
// （节假日）则按已收盘显示。时段为常规规则，不含临时休市。
const G_SESSIONS = {
  "000001": ["Asia/Shanghai", [1,2,3,4,5], [["09:30","11:30"], ["13:00","15:00"]]],
  "000300": ["Asia/Shanghai", [1,2,3,4,5], [["09:30","11:30"], ["13:00","15:00"]]],
  "399006": ["Asia/Shanghai", [1,2,3,4,5], [["09:30","11:30"], ["13:00","15:00"]]],
  "000688": ["Asia/Shanghai", [1,2,3,4,5], [["09:30","11:30"], ["13:00","15:00"]]],
  "NDX":    ["America/New_York", [1,2,3,4,5], [["09:30","16:00"]]],
  "DJIA":   ["America/New_York", [1,2,3,4,5], [["09:30","16:00"]]],
  "SPX":    ["America/New_York", [1,2,3,4,5], [["09:30","16:00"]]],
  "HSI":    ["Asia/Hong_Kong", [1,2,3,4,5], [["09:30","12:00"], ["13:00","16:00"]]],
  "HSTECH": ["Asia/Hong_Kong", [1,2,3,4,5], [["09:30","12:00"], ["13:00","16:00"]]],
  "TWII":   ["Asia/Taipei", [1,2,3,4,5], [["09:00","13:30"]]],
  "N225":   ["Asia/Tokyo", [1,2,3,4,5], [["09:00","11:30"], ["12:30","15:30"]]],
  "KS11":   ["Asia/Seoul", [1,2,3,4,5], [["09:00","15:30"]]],
  "FTSE":   ["Europe/London", [1,2,3,4,5], [["08:00","16:30"]]],
  "GDAXI":  ["Europe/Berlin", [1,2,3,4,5], [["09:00","17:30"]]],
  "FCHI":   ["Europe/Paris", [1,2,3,4,5], [["09:00","17:30"]]],
  "MIB":    ["Europe/Rome", [1,2,3,4,5], [["09:00","17:30"]]],
  "IBEX":   ["Europe/Madrid", [1,2,3,4,5], [["09:00","17:30"]]],
  "ATX":    ["Europe/Vienna", [1,2,3,4,5], [["09:00","17:30"]]],
  "SSMI":   ["Europe/Zurich", [1,2,3,4,5], [["09:00","17:30"]]],
  "STI":    ["Asia/Singapore", [1,2,3,4,5], [["09:00","12:00"], ["13:00","17:00"]]],
  "SENSEX": ["Asia/Kolkata", [1,2,3,4,5], [["09:15","15:30"]]],
  "AS51":   ["Australia/Sydney", [1,2,3,4,5], [["10:00","16:00"]]],
  "TSX":    ["America/Toronto", [1,2,3,4,5], [["09:30","16:00"]]],
  "BVSP":   ["America/Sao_Paulo", [1,2,3,4,5], [["10:00","17:00"]]],
  "MXX":    ["America/Mexico_City", [1,2,3,4,5], [["08:30","15:00"]]],
  "RTS":    ["Europe/Moscow", [1,2,3,4,5], [["10:00","18:50"]]],
  "NZ50":   ["Pacific/Auckland", [1,2,3,4,5], [["10:00","16:45"]]],
  "VNINDEX": ["Asia/Ho_Chi_Minh", [1,2,3,4,5], [["09:15","11:30"], ["13:00","14:45"]]],
  "KLSE":   ["Asia/Kuala_Lumpur", [1,2,3,4,5], [["09:00","12:30"], ["14:30","17:00"]]],
  "JKSE":   ["Asia/Jakarta", [1,2,3,4,5], [["09:00","12:00"], ["13:30","15:50"]]],
  "PSI":    ["Asia/Manila", [1,2,3,4,5], [["09:30","12:00"], ["13:30","15:30"]]],
  "SET":    ["Asia/Bangkok", [1,2,3,4,5], [["10:00","12:30"], ["14:30","16:30"]]],
  // TE 中东源（周日常为周日至周四；无实时时间戳，纯时刻表判断）
  "TE:TASI": ["Asia/Riyadh", [0,1,2,3,4], [["10:00","15:00"]]],
  "TE:ADX General": ["Asia/Dubai", [1,2,3,4,5], [["10:00","15:00"]]],
  "TE:DFM General": ["Asia/Dubai", [1,2,3,4,5], [["10:00","15:00"]]],
  "TE:BIST 100": ["Europe/Istanbul", [1,2,3,4,5], [["10:00","18:00"]]],
  "TE:TA-125": ["Asia/Jerusalem", [0,1,2,3,4], [["10:00","17:15"]]],
  "TE:QE": ["Asia/Qatar", [0,1,2,3,4], [["09:30","13:00"]]],
  "TE:MSM 30": ["Asia/Muscat", [0,1,2,3,4], [["10:00","12:00"]]],
  "TE:ASE": ["Asia/Amman", [0,1,2,3,4], [["10:00","12:00"]]],
  "TE:Kuwait All Share": ["Asia/Kuwait", [0,1,2,3,4], [["09:00","13:15"]]],
  "TE:Estirad": ["Asia/Bahrain", [0,1,2,3,4], [["09:30","13:00"]]],
  "TE:EGX 30": ["Africa/Cairo", [0,1,2,3,4], [["10:00","14:30"]]],
  "TE:CFG 25": ["Africa/Casablanca", [1,2,3,4,5], [["09:30","15:40"]]],
};
const gTzFmtCache = {};
function gLocMin(tz, now) {
  let f = gTzFmtCache[tz];
  if (!f) f = gTzFmtCache[tz] = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const p = {};
  for (const x of f.formatToParts(now)) if (x.type !== "literal") p[x.type] = x.value;
  const wd = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[p.weekday];
  return { wd: wd, min: (+p.hour) * 60 + (+p.minute) };
}
function gSchedState(code, now) {
  const cfg = G_SESSIONS[code];
  if (!cfg) return null;
  const loc = gLocMin(cfg[0], now);
  if (cfg[1].indexOf(loc.wd) === -1) return "closed";
  const hm = s => (+s.slice(0, 2)) * 60 + (+s.slice(3, 5));
  const ss = cfg[2];
  for (let i = 0; i < ss.length; i++) if (loc.min >= hm(ss[i][0]) && loc.min < hm(ss[i][1])) return "open";
  if (ss.length > 1 && loc.min >= hm(ss[0][1]) && loc.min < hm(ss[1][0])) return "lunch";
  return loc.min < hm(ss[0][0]) ? "pre" : "closed";
}
// v99 状态："open"开盘中 / "lunch"午间休市 / "pre"未开盘 / "closed"已收盘 / null 未知
function gIsOpen(it) {
  const st = gSchedState(it.code, Date.now());
  if (st === "open" && !it.te && it.time && Date.now() / 1000 - it.time > 45 * 60) return "closed";
  return st;
}
function gOpenBadge(it) {
  const st = gIsOpen(it);
  if (st === null) return "<span style='color:#8a94a6;font-size:11px'>快照</span>";
  if (st === "open") return "<span style='color:#3dd68c;font-size:11px'>● 开盘中</span>";
  if (st === "lunch") return "<span style='color:#f6ad55;font-size:11px'>☕ 午间休市</span>";
  if (st === "pre") return "<span style='color:#6ea8fe;font-size:11px'>○ 未开盘</span>";
  return "<span style='color:#5c6678;font-size:11px'>○ 已收盘</span>";
}
function gRegionOpen(arr) {
  // 国家维度取最强状态：开盘中 > 午休 > 未开盘 > 已收盘；全部未知返回 null
  const rank = { open: 4, lunch: 3, pre: 2, closed: 1 };
  let best = null;
  for (const it of arr) {
    const o = gIsOpen(it);
    if (o === null) continue;
    if (best === null || rank[o] > rank[best]) best = o;
  }
  return best;
}
function gRangeVal(it) {
  const field = RANGES.find(r => r.id === currentRange).field;
  const v = it[field];
  return (v === null || v === undefined) ? null : v;
}
function gPctStr(v) {
  if (v === null || v === undefined) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
}
function gPctColor(v) {
  if (v === null || v === undefined) return "#8a94a6";
  return v >= 0 ? "#ff5c5c" : "#3dd68c";
}
function setGStatus(text, cls) {
  const el = document.getElementById("gStatus");
  if (!el) return;
  el.textContent = text;
  el.className = cls || "";
}
function gFmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = n => (n < 10 ? "0" : "") + n;
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
function gFmtPrice(v) {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? v.toFixed(2) : v.toFixed(2);
}
function setFStatus(text, cls) {
  const el = document.getElementById("fStatus");
  if (!el) return;
  el.textContent = text;
  el.className = cls || "";
}
function fNorm(v) {
  if (v === null || v === undefined || v === "-" || v === "") return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}
function buildMapData() {
  const data = [];
  for (const [region, arr] of gCountryMap) {
    const vals = arr.map(gRangeVal).filter(v => v !== null && v !== undefined);
    if (!vals.length) continue;
    const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
    data.push({ name: region, value: +avg.toFixed(2) });
  }
  return data;
}
let gMapAspectCache = null;
// v95 world.json 投影后宽高比：ECharts 默认 aspectScale=0.75，投影宽=经度跨度×0.75、高=纬度跨度
function gMapAspect() {
  if (gMapAspectCache) return gMapAspectCache;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const f of (gWorld && gWorld.features) || []) {
    const g = f.geometry;
    if (!g) continue;
    const polys = g.type === "Polygon" ? [g.coordinates] : (g.type === "MultiPolygon" ? g.coordinates : []);
    for (const poly of polys) for (const ring of poly) for (const p of ring) {
      if (p[0] < minX) minX = p[0]; if (p[0] > maxX) maxX = p[0];
      if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1];
    }
  }
  gMapAspectCache = (maxX > minX && maxY > minY) ? (maxX - minX) * 0.75 / (maxY - minY) : 1.94;
  return gMapAspectCache;
}
// v95 修复手机竖屏长宽比失真：原来用 top/bottom/left/right 让 ECharts 把地图各向异性拉伸铺满容器矩形，
// 竖屏矩形瘦高时大陆被横向压扁约七成。改用 layoutCenter+layoutSize 等比缩放（contain）：
// 容器相对更宽 → 按高度定尺寸，底部留 48px 给 visualMap（桌面观感不变）；
// 容器相对更窄 → 按宽度定尺寸，竖屏整图完整可见，比例不再失真（可拖拽/双指缩放）
function gMapLayout() {
  if (!gChart) return { layoutCenter: ["50%", "50%"], layoutSize: "100%" };
  const w = gChart.getWidth(), h = gChart.getHeight();
  if (!w || !h) return { layoutCenter: ["50%", "50%"], layoutSize: "100%" };
  const aspect = gMapAspect();
  if (w / h >= aspect) {
    const vh = Math.max(80, h - 48);
    return { layoutCenter: [Math.round(w / 2), Math.round(vh / 2)], layoutSize: Math.round(vh * aspect) };
  }
  return { layoutCenter: [Math.round(w / 2), Math.round(h / 2)], layoutSize: Math.round(w) };
}
// v97 触屏设备（主指针为粗指针）：roam 用 "move"（仅平移），双指缩放由 initGlobalChart 里的自管 pinch 接管
const COARSE_POINTER = window.matchMedia ? window.matchMedia("(pointer: coarse)").matches : false;
// 双指缩放灵敏度：1 = 完全跟随双指距离比例；<1 更迟钝（ECharts 内置是每次事件固定 ×1.1，过灵敏）
const MAP_PINCH_SENSITIVITY = 0.7;
function globalOption() {
  const maxAbs = gMaxAbs();
  return {
    backgroundColor: "#0e1420",
    tooltip: {
      trigger: "item", backgroundColor: "rgba(22,27,38,.97)", borderColor: "#3b5077",
      textStyle: { color: "#e8ecf4", fontSize: 13 },
      formatter: p => {
        const arr = gCountryMap.get(p.name) || [];
        if (!arr || !arr.length) return "<b>" + p.name + "</b><br>无指数数据";
        const rows = arr.slice(0, 6).map(i => {
          const v = gRangeVal(i), c = gPctColor(v);
          return "<div style='margin:3px 0'><span style='color:" + c + "'>" + i.name + "</span> " +
            "<b>" + gFmtPrice(i.price) + "</b> <span style='color:" + c + "'>" + gPctStr(v) + "</span> " + gOpenBadge(i) + "</div>";
        }).join("");
        const more = arr.length > 6 ? "<div style='color:#8a94a6;margin-top:4px'>…共 " + arr.length + " 条，点击查看全部</div>" : "";
        const ro = gRegionOpen(arr);
        const rBadge = ro === null ? "" :
          (ro === "open" ? " <span style='color:#3dd68c;font-size:11px'>● 开盘中</span>" :
           ro === "lunch" ? " <span style='color:#f6ad55;font-size:11px'>☕ 午间休市</span>" :
           ro === "pre" ? " <span style='color:#6ea8fe;font-size:11px'>○ 未开盘</span>" :
           " <span style='color:#5c6678;font-size:11px'>○ 已收盘</span>");
        return "<div style='font-weight:700;margin-bottom:4px'>" + p.name + " · " + arr[0].country + rBadge + "</div>" + rows + more;
      }
    },
    visualMap: {
      min: -maxAbs, max: maxAbs, calculable: false, show: true,
      orient: "horizontal", left: "center", bottom: 8, itemWidth: 12, itemHeight: 90,
      inRange: { color: ["#17c964", "#2b3547", "#f31260"] },
      text: ["涨", "跌"], textStyle: { color: "#8a94a6" }
    },
    series: [{
      type: "map", map: "world", roam: COARSE_POINTER ? "move" : true, zoom: 1.12, ...gMapLayout(),
      // 缩放范围限制：0.8 防止缩出世界外，12 供细看小国
      scaleLimit: { min: 0.8, max: 12 },
      label: { show: false },
      emphasis: { label: { show: true, color: "#fff", fontSize: 13 }, itemStyle: { areaColor: "#3b6ea5" } },
      itemStyle: { areaColor: "#1e2530", borderColor: "#3b5077", borderWidth: 0.6 },
      data: buildMapData()
    }]
  };
}
function showCountryPanel(region, arr) {
  document.getElementById("cpTitle").textContent = region + " · " + arr[0].country + "（" + arr.length + " 个指数）";
  document.getElementById("cpList").innerHTML = arr.map(i => {
    const v = gRangeVal(i), c = gPctColor(v);
    const chg = (i.chg === null || i.chg === undefined) ? "—" : (i.chg >= 0 ? "+" : "") + i.chg.toFixed(2);
    return "<div class='g-row'><span class='g-name' title='" + i.name + "'>" + i.name + "</span>" +
      "<span class='g-price'>" + gFmtPrice(i.price) + "</span>" +
      "<span class='g-pct' style='color:" + c + "'>" + gPctStr(v) + "</span>" +
      "<span class='g-chg' style='color:" + c + "'>" + chg + "</span>" +
      "<span class='g-open'>" + gOpenBadge(i) + "</span>" +
      "<span class='g-time'>" + gFmtTime(i.time) + "</span></div>";
  }).join("");
  document.getElementById("countryPanel").classList.remove("hidden");
  gPanelOpen = true;
}
async function initGlobalChart() {
  if (gChart) return;
  const el = document.getElementById("gmap");
  if (!el || !window.echarts) return;
  gChart = echarts.init(el);
  try {
    const r = await fetch("map/world.json?t=" + Date.now());
    gWorld = await r.json();
    echarts.registerMap("world", gWorld);
  } catch (e) {
    gChart.dispose(); gChart = null;
    showToast("世界地图数据加载失败");
    return;
  }
  gChart.setOption(globalOption(), true);
  gChart.on("click", p => {
    const arr = gCountryMap.get(p.name);
    if (arr && arr.length) {
      lastPanelRegion = p.name;
      showCountryPanel(p.name, arr);
    } else {
      // 点击无数据区域/空白：关闭面板
      document.getElementById("countryPanel").classList.add("hidden");
      gPanelOpen = false;
    }
  });
  // v97 触屏双指缩放自管：ECharts 内置 pinch 每个事件固定 ×1.1（与手指距离无关），
  // 事件频率越高缩得越猛（120Hz 屏尤甚），手机上体感过灵敏。
  // 此处按双指实际距离增量比（e.pinchScale）等比缩放，乘灵敏度系数。
  const zr = gChart.getZr();
  if (COARSE_POINTER) {
    const sid = gChart.getModel().getSeriesByIndex(0).id;
    let lastPinchT = 0;
    zr.on("pinch", e => {
      gMapDragging = true;   // v98 pinch 期间同样推迟数据刷新
      const ev = (e.event && e.event.pinchScale !== undefined) ? e.event : e;
      const ratio = ev.pinchScale;
      const now = Date.now();
      // 距上次事件超过 350ms = 新手势第一帧（识别器拿上一手势旧点会算出垃圾比例），跳过不缩放
      if (now - lastPinchT > 350) { lastPinchT = now; return; }
      lastPinchT = now;
      if (!isFinite(ratio) || ratio <= 0.4 || ratio >= 2.5 || ratio === 1) return;
      const step = Math.pow(ratio, MAP_PINCH_SENSITIVITY);
      gChart.dispatchAction({ type: "geoRoam", seriesId: sid, zoom: step, originX: ev.pinchX, originY: ev.pinchY });
    });
  }
  // v98 拖拽门控信号：触屏 touchstart/touchend 分别合成 zr mousedown/mouseup（见 lib HandlerProxy touch 分支）
  zr.on("mousedown", () => { gMapDragging = true; gMapDragStart = Date.now(); });
  zr.on("mouseup", () => { gMapDragging = false; gMapFlushPending(); });
}
function gMaxAbs() {
  // 固定 ±3% 区间：颜色对比稳定（全球指数日内极少超 ±3%），避免被极端值拉伸导致颜色趋同
  return 3;
}
// v98 拖拽门控：拖拽进行中推迟数据刷新的全量重渲染（触屏 touchstart/touchend 会合成 zr mousedown/mouseup，见 lib HandlerProxy touch 分支）；
// 全量重绘手机上耗百毫秒级且更新动画每帧叠加，与拖拽同帧会冻结触控（断触感），松手后补刷
let gMapDragging = false;
let gMapDragStart = 0;
let gMapRefreshPending = false;
function gMapFlushPending() {
  if (gMapRefreshPending) { gMapRefreshPending = false; renderGlobalMap(); }
}
// v96 尺寸变化后统一重适配：resize() 只按 model 里的旧布局参数适配，必须随后补上重算的布局
function gRefitChart() {
  if (!gChart) return;
  gChart.resize();
  gChart.setOption({ series: [gMapLayout()] });
}
function renderGlobalMap() {
  if (!gChart || !gWorld) return;
  // merge 模式局部更新（notMerge=true 会重建配置树导致地图黑屏）
  const maxAbs = gMaxAbs();
  // v96 只更新数据，绝不动 layoutCenter/layoutSize：布局参数每次 merge 都会触发 ECharts 重新适配，
  // 容器尺寸若在两次刷新之间变过（地址栏/旋转/页面缩放），地图就会在数据刷新瞬间跳位
  // v98 拖拽中推迟（10s 兜底：touchcancel 等异常丢 mouseup 时恢复刷新，避免数据永久冻结）
  if (gMapDragging && Date.now() - gMapDragStart < 10000) { gMapRefreshPending = true; return; }
  gChart.setOption({
    animation: false,   // v98 数据更新直接切换颜色：默认 300ms 过渡动画期间每帧全量重绘，拖拽时叠加成断触
    visualMap: { min: -maxAbs, max: maxAbs },
    series: [{ data: buildMapData() }]
  });
  if (gPanelOpen) {
    const sel = gCountryMap.get(lastPanelRegion);
    if (sel) showCountryPanel(lastPanelRegion, sel);
  }
}
let lastPanelRegion = null;
async function loadGlobalData() {
  try {
    const r = await fetch("global.json?t=" + Date.now());
    const j = await r.json();
    gItems = (j && j.items) || [];
  } catch (e) {
    gItems = [];
    setGStatus("全球实时：global.json 加载失败", "err");
  }
  gCountryMap = new Map();
  for (const it of gItems) {
    if (!gCountryMap.has(it.region)) gCountryMap.set(it.region, []);
    gCountryMap.get(it.region).push(it);
  }
  startGlobalLive();  // 独立实时循环（无论 A 股是否收盘、复选框是否勾选）
}
async function fetchGlobalQuotes() {
  if (!gItems.length) {
    setGStatus("全球实时：数据未加载", "err");
    return;
  }
  // v91：东财实时循环只请求有 secid 的条目；TE 中东源（te:1、无 secid）由 global.json 快照供数
  const secids = gItems.filter(i => i.secid).map(i => i.secid); // v84 A股指数用真实 secid（旧数据无 secid 回退 100.）
  try {
    const url = QUOTE_URL + "?fltt=2&pn=1&pz=40&np=1&fields=f2,f3,f4,f12,f109,f160,f110,f24,f25,f124&secids=" + secids.join(",");
    const res = await fetch(url);
    if (!res.ok) {
      setGStatus("全球实时：HTTP " + res.status, "err");
      return;
    }
    const j = await res.json();
    const diff = (j && j.data && j.data.diff) || [];
    setGStatus("全球实时：✓ " + new Date().toLocaleTimeString() + " 收到 " + diff.length + " 条", "ok");
    const byCode = {};
    for (const x of diff) byCode[x.f12] = x;
    let changed = false;
    for (const it of gItems) {
      const x = byCode[it.code];
      if (!x) continue;
      const nu = { price: gNorm(x.f2), pct: gNorm(x.f3), chg: gNorm(x.f4),
        pct5: gNorm(x.f109), pct10: gNorm(x.f160), pct20: gNorm(x.f110),
        pct60: gNorm(x.f24), pctYTD: gNorm(x.f25), time: x.f124 };
      if (JSON.stringify(nu) !== JSON.stringify({ price: it.price, pct: it.pct, chg: it.chg,
        pct5: it.pct5, pct10: it.pct10, pct20: it.pct20, pct60: it.pct60, pctYTD: it.pctYTD, time: it.time })) {
        changed = true;
        Object.assign(it, nu);
      }
    }
    if (changed && gChart && gWorld) renderGlobalMap();
  } catch (e) {
    setGStatus("全球实时：失败 " + (e && e.message ? e.message : e), "err");
  }
}
// 全球指数独立实时：不受 A 股交易时段/自动刷新复选框限制，页面加载即启动
let gLiveTimer = null;
function startGlobalLive() {
  if (gLiveTimer) return;
  fetchGlobalQuotes();  // 立即先刷一次（让状态栏尽快出结果）
  gLiveTimer = setInterval(() => fetchGlobalQuotes(), QUOTE_INTERVAL_MS);
}

// ===== 期货视图（futures.json + 8 秒实时，独立于 A 股时段） =====
async function loadFuturesData() {
  try {
    const r = await fetch("futures.json?t=" + Date.now());
    const j = await r.json();
    fItems = (j && j.items) || [];
  } catch (e) {
    fItems = [];
    setFStatus("期货实时：futures.json 加载失败", "err");
  }
  renderFutures();
  startFuturesLive();
}
async function fetchFuturesQuotes() {
  if (!fItems.length) {
    setFStatus("期货实时：数据未加载", "err");
    return;
  }
  const secids = fItems.map(i => i.secid);
  try {
    const url = QUOTE_URL + "?fltt=2&pn=1&pz=40&np=1&fields=f2,f3,f4,f12,f124&secids=" + secids.join(",");
    const res = await fetch(url);
    if (!res.ok) {
      setFStatus("期货实时：HTTP " + res.status, "err");
      return;
    }
    const j = await res.json();
    const diff = (j && j.data && j.data.diff) || [];
    setFStatus("期货实时：✓ " + new Date().toLocaleTimeString() + " 收到 " + diff.length + " 条", "ok");
    const byCode = {};
    for (const x of diff) byCode[x.f12] = x;
    let changed = false;
    for (const it of fItems) {
      const x = byCode[it.code];
      if (!x) continue;
      const nu = { price: fNorm(x.f2), pct: fNorm(x.f3), chg: fNorm(x.f4), time: x.f124 };
      if (nu.price !== it.price || nu.pct !== it.pct || nu.chg !== it.chg || nu.time !== it.time) {
        changed = true;
        Object.assign(it, nu);
      }
    }
    if (changed) renderFutures();
  } catch (e) {
    setFStatus("期货实时：失败 " + (e && e.message ? e.message : e), "err");
  }
}
function startFuturesLive() {
  if (fLiveTimer) return;
  fetchFuturesQuotes();  // 立即先刷一次
  fLiveTimer = setInterval(() => fetchFuturesQuotes(), QUOTE_INTERVAL_MS);
}
function renderFutures() {
  const el = document.getElementById("fList");
  if (!el) return;
  const groups = [];
  for (const it of fItems) {
    let g = groups.find(x => x.name === it.group);
    if (!g) { g = { name: it.group, items: [] }; groups.push(g); }
    g.items.push(it);
  }
  const cardsHtml = groups.map(g => {
    const rows = g.items.map(it => {
      const pct = it.pct === null || it.pct === undefined ? null : it.pct;
      const cls = pct === null ? "flat" : (pct > 0 ? "up" : pct < 0 ? "down" : "flat");
      const pctTxt = pct === null ? "—" : (pct > 0 ? "+" : "") + pct.toFixed(2) + "%";
      const chg = it.chg === null || it.chg === undefined ? null : it.chg;
      const chgTxt = chg === null ? "—" : (chg > 0 ? "+" : "") + chg.toFixed(2);
      const price = it.price === null || it.price === undefined ? "—" : it.price.toFixed(2);
      const url = it.url || (String(it.secid || "").startsWith("171.") ? "https://wap.eastmoney.com/quote/stock/" + it.secid + ".html" : ("https://quote.eastmoney.com/" + ((String(it.secid || "").startsWith("101.") || String(it.secid || "").startsWith("109.") || String(it.secid || "").startsWith("112.")) ? "globalfuture" : "qihuo") + "/" + it.code + ".html"));
      return '<a class="fg-row" href="' + url + '" target="_blank" rel="noopener"><span class="fg-name">' + it.name +
        '<span class="fg-code"> ' + it.code + '</span></span>' +
        '<span class="fg-price">' + price + '</span>' +
        '<span class="fg-chg">' + chgTxt + '</span>' +
        '<span class="fg-pct ' + cls + '">' + pctTxt + '</span></a>';
    }).join("");
    return '<div class="fg-card"><div class="fg-head">' + g.name +
      '<span class="cnt">' + g.items.length + ' 个品种</span></div>' + rows + '</div>';
  });
  // v36c：两列紧凑布局——列内组间距统一 12px，列间按估算高度贪心均衡（保持组顺序）；
  // 窄屏（≤900px）回单列原序
  if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
    el.innerHTML = '<div class="fg-col">' + cardsHtml.join("") + '</div>';
  } else {
    const colHtml = ["", ""];
    const colH = [0, 0];
    for (let i = 0; i < groups.length; i++) {
      const h = 31 + groups[i].items.length * 28; // 组标题 + 行数×行高
      const ci = colH[0] <= colH[1] ? 0 : 1;
      colHtml[ci] += cardsHtml[i];
      colH[ci] += h + 12;
    }
    el.innerHTML = '<div class="fg-col">' + colHtml[0] + '</div><div class="fg-col">' + colHtml[1] + '</div>';
  }
}

// ---------- 现货模块（v31：秦皇岛港Q5500，Mysteel 日更指数，8 秒轮询 data.json） ----------
let spotData = null, spotUpdating = false, spotCooldownUntil = 0;
function setSpotMsg(txt, cls) {
  const el = document.getElementById("spotMsg");
  if (el) { el.textContent = txt; el.className = "spot-msg" + (cls ? " " + cls : ""); }
}
function spotCls(s) {
  if (s == null || s === "" || s === "-") return "flat";
  const n = parseFloat(String(s).replace("%", "").trim());
  if (isNaN(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}
async function fetchSpot() {
  try {
    const j = freshPayload() || await loadDataJson();  // v98: 现货为日更数据，复用最近快照即可，不必整包重拉
    spotData = j && j.q5500 ? j.q5500 : null;
    renderSpot();
  } catch (e) {
    setSpotMsg("现货：data.json 加载失败", "err");
  }
}
function renderSpot() {
  const d = spotData;
  const el = id => document.getElementById(id);
  const val = el("spotVal"), day = el("spotDay"), date = el("spotDate"),
    lastday = el("spotLastday"), ds = el("spotDayScale"), wk = el("spotWeek"),
    ws = el("spotWeekScale"), mo = el("spotMonth"), ms = el("spotMonthScale"),
    yr = el("spotYear"), ys = el("spotYearScale");
  if (!val) return;
  if (!d || !d.value) {
    val.textContent = "—"; val.className = "spot-val flat";
    day.textContent = ""; date.textContent = "";
    for (const x of [lastday, ds, wk, ws, mo, ms, yr, ys]) x.textContent = "—";
    for (const x of [ds, ws, ms, ys]) x.className = "sr-p flat";
    setSpotMsg("未查询到现货数据（可点击「更新现货」抓取 Mysteel 最新指数）", "warn");
    return;
  }
  const dsc = spotCls(d.lastDayScale);
  val.textContent = d.value;
  val.className = "spot-val " + dsc;
  day.textContent = "元/吨 · " + (d.date || "—");
  date.textContent = "数据日期 " + (d.date || "—") + (d.fetched_at ? "（抓取 " + d.fetched_at + "）" : "");
  lastday.textContent = d.lastdayValue || "—";
  ds.textContent = d.lastDayScale || "—"; ds.className = "sr-p " + spotCls(d.lastDayScale);
  wk.textContent = d.lastWeekValue || "—";
  ws.textContent = d.lastWeekScale || "—"; ws.className = "sr-p " + spotCls(d.lastWeekScale);
  mo.textContent = d.lastMonthValue || "—";
  ms.textContent = d.lastMonthScale || "—"; ms.className = "sr-p " + spotCls(d.lastMonthScale);
  yr.textContent = d.lastYearValue || "—";
  ys.textContent = d.lastYearScale || "—"; ys.className = "sr-p " + spotCls(d.lastYearScale);
  setSpotMsg("数据来源：" + (d.source || "Mysteel 秦皇岛港Q5500动力煤指数") + " · 进入页面或点击「更新现货」时刷新", "ok");
}
async function updateSpot() {
  if (spotUpdating) return;
  if (Date.now() < spotCooldownUntil) {
    const s = Math.ceil((spotCooldownUntil - Date.now()) / 1000);
    setSpotMsg("更新冷却中，" + s + " 秒后再试（防频繁请求触发风控）", "warn");
    return;
  }
  const btn = document.getElementById("btnSpot");
  spotUpdating = true;
  if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
  setSpotMsg("正在抓取 Mysteel 最新指数…", "");
  try {
    const res = await fetch("/api/update?t=" + Date.now());
    const j = await res.json();
    if (j && j.ok) {
      spotData = j.q5500 || null;
      renderSpot();
      spotCooldownUntil = Date.now() + 60000;
      setSpotMsg("更新成功" + (j.time ? "（" + j.time + "）" : "") + "，60 秒内不可重复更新", "ok");
    } else if (j && j.cooldown) {
      setSpotMsg("更新冷却中，" + j.cooldown + " 秒后再试（防频繁请求触发风控）", "warn");
    } else {
      setSpotMsg("更新失败：" + ((j && j.msg) || "未知错误"), "err");
    }
  } catch (e) {
    setSpotMsg("更新失败：" + (e && e.message ? e.message : e), "err");
  }
  spotUpdating = false;
  if (btn) { btn.disabled = false; btn.textContent = "更新现货"; }
}
function startSpotLive() {
  fetchSpot();
}

// ---------- 稀土现货模块（v83：氧化镨钕/氧化镝，中国稀土网 cre.net 日更报价） ----------
let reData = null, reUpdating = false, reCooldownUntil = 0;
function setReMsg(txt, cls) {
  const el = document.getElementById("reMsg");
  if (el) { el.textContent = txt; el.className = "spot-msg" + (cls ? " " + cls : ""); }
}
async function fetchRe() {
  try {
    const j = freshPayload() || await loadDataJson();  // v98: 同 fetchSpot，复用最近快照
    reData = j && j.rare_earth ? j.rare_earth : null;
    renderRe();
  } catch (e) {
    setReMsg("稀土：data.json 加载失败", "err");
  }
}
function renderRe() {
  const d = reData;
  const el = id => document.getElementById(id);
  const pv = el("rePrNd"), pc = el("rePrNdChg"), ps = el("rePrNdSub"),
    dv = el("reDy"), dc = el("reDyChg"), ds = el("reDySub"), date = el("reDate");
  if (!pv) return;
  if (!d || (!d.pr_nd && !d.dy)) {
    pv.textContent = "—"; dv.textContent = "—";
    if (pc) { pc.textContent = "—"; pc.className = "re-chg flat"; }
    if (dc) { dc.textContent = "—"; dc.className = "re-chg flat"; }
    if (ps) ps.textContent = "—";
    if (ds) ds.textContent = "—";
    if (date) date.textContent = "";
    setReMsg("未查询到稀土数据（可点击「更新稀土」抓取 cre.net 最新报价）", "warn");
    return;
  }
  const fmtNum = n => (n == null || isNaN(n)) ? "—" : String(n);
  const chgHtml = n => {
    if (n == null || isNaN(n) || n === 0) return '<span class="re-chg flat">0.00</span>';
    return '<span class="re-chg ' + (n > 0 ? "up" : "down") + '">' + (n > 0 ? "+" : "") + n.toFixed(2) + "</span>";
  };
  if (d.pr_nd) {
    const p = d.pr_nd;
    pv.textContent = fmtNum(p.avg) + " " + (p.unit || "元/千克");
    pv.className = "spot-val " + (p.change > 0 ? "up" : (p.change < 0 ? "down" : "flat"));
    pc.innerHTML = chgHtml(p.change);
    ps.textContent = "区间 " + fmtNum(p.low) + " ~ " + fmtNum(p.high) + " · " + (p.spec || "");
  } else {
    pv.textContent = "—"; pc.innerHTML = '<span class="re-chg flat">—</span>'; ps.textContent = "页面暂无该品种";
  }
  if (d.dy) {
    const y = d.dy;
    dv.textContent = fmtNum(y.avg) + " " + (y.unit || "元/千克");
    dv.className = "spot-val " + (y.change > 0 ? "up" : (y.change < 0 ? "down" : "flat"));
    dc.innerHTML = chgHtml(y.change);
    ds.textContent = "区间 " + fmtNum(y.low) + " ~ " + fmtNum(y.high) + " · " + (y.spec || "");
  } else {
    dv.textContent = "—"; dc.innerHTML = '<span class="re-chg flat">—</span>'; ds.textContent = "页面暂无该品种";
  }
  if (date) date.textContent = "数据日期 " + (d.date || "—") + (d.fetched_at ? "（抓取 " + d.fetched_at + "）" : "");
  setReMsg("数据来源：" + (d.source || "中国稀土网 (www.cre.net)") + " · 点击「更新稀土」刷新", "ok");
}
async function updateRe() {
  if (reUpdating) return;
  if (Date.now() < reCooldownUntil) {
    const s = Math.ceil((reCooldownUntil - Date.now()) / 1000);
    setReMsg("更新冷却中，" + s + " 秒后再试（防频繁请求触发风控）", "warn");
    return;
  }
  const btn = document.getElementById("btnRe");
  reUpdating = true;
  if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
  setReMsg("正在抓取 cre.net 最新报价…", "");
  try {
    const res = await fetch("/api/rare_earth_refetch?t=" + Date.now());
    const j = await res.json();
    if (j && j.ok) {
      reData = j.rare_earth || null;
      renderRe();
      reCooldownUntil = Date.now() + 60000;
      setReMsg("更新成功" + (j.time ? "（" + j.time + "）" : "") + "，60 秒内不可重复更新", "ok");
    } else if (j && j.cooldown) {
      setReMsg("更新冷却中，" + j.cooldown + " 秒后再试（防频繁请求触发风控）", "warn");
    } else {
      setReMsg("更新失败：" + ((j && j.msg) || "未知错误"), "err");
    }
  } catch (e) {
    setReMsg("更新失败：" + (e && e.message ? e.message : e), "err");
  }
  reUpdating = false;
  if (btn) { btn.disabled = false; btn.textContent = "更新稀土"; }
}
function startReLive() {
  fetchRe();
}

function switchView(v) {
  view = v;
  const tabA = document.getElementById("tabA"), tabG = document.getElementById("tabGlobal"), tabF = document.getElementById("tabFutures"), tabC = document.getElementById("tabCal");
  const wrap = document.getElementById("wrap"), gv = document.getElementById("globalView"), fv = document.getElementById("futuresView"), cv = document.getElementById("calView");
  tabA.classList.toggle("on", v === "a");
  tabG.classList.toggle("on", v === "global");
  if (tabF) tabF.classList.toggle("on", v === "futures");
  if (tabC) tabC.classList.toggle("on", v === "cal");
  wrap.classList.toggle("v-hidden", v !== "a");
  gv.classList.toggle("gv-hidden", v !== "global");
  if (fv) fv.classList.toggle("fv-hidden", v !== "futures");
  if (cv) cv.classList.toggle("cal-hidden", v !== "cal");
  crumbEl.style.display = (v === "a") ? "" : "none";
  // 区间选择器只对 A 股云图/全球地图有意义，期货/日历视图隐藏
  const rangeSelEl = document.getElementById("rangeSel");
  if (rangeSelEl) rangeSelEl.style.display = (v === "a" || v === "global") ? "" : "none";
  // 搜索框仅服务 A 股云图（goToStock/goToSector 下钻云图，不切视图）→ 只在 a 视图显示
  const searchBoxEl = document.querySelector(".searchbox");
  if (searchBoxEl) searchBoxEl.style.display = (v === "a") ? "" : "none";
  if (v === "cal") calMarkSeen();
  if (v === "a") {
    // 切回 A 股：确保画布尺寸有效并重绘（防止隐藏期间画布被清空导致空白）
    sizeCanvas();
    draw();
  }
  if (v === "global") {
    if (!gChart) initGlobalChart();
    else { renderGlobalMap(); setTimeout(gRefitChart, 30); }
  }
  if (v === "futures") { fetchSpot(); if (fItems.length) renderFutures(); }
  if (v === "cal") { if (!calItems) fetchCalendar(); else renderCalendar(); }
}

async function refreshQuotes() {
  if (!root) return;
  if (!codeIndex) buildCodeIndex();
  const secids = [];
  for (const sec of root.children) {
    for (const ind of sec.children) {
      for (const st of ind.children) {
        secids.push(st.code.startsWith("6") ? "1." + st.code : "0." + st.code);
      }
    }
  }
  const batches = [];
  for (let i = 0; i < secids.length; i += QUOTE_BATCH) batches.push(secids.slice(i, i + QUOTE_BATCH));
  let okBatches = 0;
  for (let i = 0; i < batches.length; i += QUOTE_CONCURRENCY) {
    const chunk = batches.slice(i, i + QUOTE_CONCURRENCY);
    const results = await Promise.all(chunk.map(b => fetchQuotesBatch(b).catch(() => null)));
    for (const diff of results) {
      if (!diff || diff.length === 0) continue;
      okBatches++;
      for (const x of diff) {
        const st = codeIndex.get(x.f12);
        if (!st) continue;
        if (x.f2 !== null && x.f2 !== undefined && x.f2 !== "-") st.price = Number(x.f2);
        if (x.f3 !== null && x.f3 !== undefined && x.f3 !== "-") st.pct = Number(x.f3);
        if (x.f4 !== null && x.f4 !== undefined && x.f4 !== "-") st.chg = Number(x.f4);
        if (x.f6 !== null && x.f6 !== undefined && x.f6 !== "-") st.amount = Number(x.f6);
        if (x.f109 !== null && x.f109 !== undefined && x.f109 !== "-") st.pct5 = Number(x.f109);
        if (x.f160 !== null && x.f160 !== undefined && x.f160 !== "-") st.pct10 = Number(x.f160);
        if (x.f110 !== null && x.f110 !== undefined && x.f110 !== "-") st.pct20 = Number(x.f110);
        if (x.f24 !== null && x.f24 !== undefined && x.f24 !== "-") st.pct60 = Number(x.f24);
        if (x.f25 !== null && x.f25 !== undefined && x.f25 !== "-") st.pctYTD = Number(x.f25);
      }
    }
  }
  if (okBatches === 0) {
    liveFailures++;
    if (liveFailures >= 3) { stopLive(); flashRefresh("实时已降级"); }
    return;
  }
  liveFailures = 0;
  lastLiveAt = Date.now();   // v96：标记旧树价格比其后落盘的快照新，fetchOnce 合并判据
  // 行业/大盘涨跌幅按最新个股重算（v90 三层：二级→一级→root；面积保持快照市值不变；6 个区间一并重算）
  for (const sec of root.children) {
    for (const ind of sec.children) recalcNodePcts(ind, ind.children);
    recalcNodePcts(sec, sec.children.reduce((a, ind) => a.concat(ind.children), []));
  }
  recalcNodePcts(root, root.children.reduce((a, sec) => a.concat(sec.children.reduce((y, ind) => y.concat(ind.children), [])), []));
  relayout();
  draw();
  updateLiveState();
  fetchIndices(); // 指数一并 8 秒刷新
  if (watchlist.length > 0) fetchWatchlistQuotes();  // 自选行情一并 8 秒刷新
}

function startLive() {
  if (liveTimer) return;
  liveTimer = setInterval(() => { refreshQuotes(); }, QUOTE_INTERVAL_MS);
  liveMode = true;
  updateLiveState();
}

function stopLive() {
  clearInterval(liveTimer);
  liveTimer = null;
  liveMode = false;
  updateLiveState();
}

function updateLiveState() {
  const el = document.getElementById("statLive");
  if (!el) return;
  if (liveMode) {
    el.textContent = "实时 8s · " + new Date().toTimeString().slice(0, 8);
  } else if (liveFailures >= 3) {
    el.textContent = "实时已降级(60s)";
  } else if (!isTradingTime()) {
    el.textContent = "已收盘";
  } else {
    el.textContent = "实时待启";
  }
}

function tickLive() {
  if (!autoRefreshEl.checked) return;
  const force = new URLSearchParams(location.search).has("live");
  if (!force && !isTradingTime()) {
    if (liveMode) stopLive();
    return;
  }
  if (liveFailures >= 3) {
    // 降级中：每 5 分钟悄悄重试一次，成功自动恢复
    if (Date.now() - lastLiveAttempt > 5 * 60 * 1000) {
      lastLiveAttempt = Date.now();
      refreshQuotes();
    }
    return;
  }
  if (!liveMode) {
    startLive();
    refreshQuotes();
  }
}

function renderIndices(json) {
  const el = document.getElementById("indices");
  if (!el || !json || !json.data || !json.data.diff) return;
  const diff = json.data.diff;
  if (!Array.isArray(diff) || diff.length === 0) return;
  el.innerHTML = diff.filter(d => String(d.f12) !== "399001").map(d => { // 深证成指仅用于计算两市成交额，不渲染
    const name = d.f14 || "—";
    const v = (d.f2 === null || d.f2 === undefined) ? "—" : Number(d.f2).toFixed(2);
    const pct = (d.f3 === null || d.f3 === undefined || isNaN(d.f3)) ? null : Number(d.f3);
    const pctStr = pct === null ? "—" : ((pct > 0 ? "+" : "") + pct.toFixed(2) + "%");
    const cls = pct === null ? "flat" : (pct >= 0 ? "up" : "down");
    return '<div class="idx"><span class="n">' + name + '</span><span class="v">' + v +
      '</span><span class="p ' + cls + '">' + pctStr + '</span></div>';
  }).join("");
  // 两市成交额：上证(f6 沪市) + 深证成指(f6 深市)，仅计算不渲染指数本体
  const sh = diff.find(d => String(d.f12) === "000001");
  const sz = diff.find(d => String(d.f12) === "399001");
  const to = document.getElementById("statTurnover");
  if (to) {
    if (sh && sz && sh.f6 != null && sz.f6 != null) {
      const total = Number(sh.f6) + Number(sz.f6);
      const txt = total >= 1e12
        ? "两市成交 " + (total / 1e12).toFixed(2) + " 万亿"
        : "两市成交 " + (total / 1e8).toFixed(0) + " 亿";
      to.textContent = txt;
      to.title = "沪深两市合计成交额（8 秒实时刷新）";
    } else {
      to.textContent = "两市成交 —";
    }
  }
}

function updateStats() {
  statTime.textContent = "数据时间 " + (fetchedAt || "—");
  statCount.textContent = totalStocks + " 只 · " + totalIndustries + " 个行业";
}

function setRefreshText(text) {
  btnRefresh.textContent = text;
  const mm = document.getElementById("mmRefresh");
  if (mm) mm.textContent = text;
}
function flashRefresh(text, ms) {
  setRefreshText(text);
  setTimeout(() => setRefreshText("全量更新"), ms || 1200);
}
function refetchData() {
  if (btnRefresh.disabled) return;
  btnRefresh.disabled = true;
  setRefreshText("抓取中…");
  showToast("全量抓取已开始，约需 1-3 分钟，完成后会提示");
  fetch("/api/refetch", { cache: "no-store" })
    .then(r => r.json())
    .then(j => {
      if (j && j.ok) { flashRefresh("更新完成", 2500); showToast("全量更新完成"); fetchData(); }
      else { const m = (j && j.msg) ? j.msg : "抓取失败"; flashRefresh(m, 3500); showToast(m); }
    })
    .catch(() => { flashRefresh("服务不可用", 2500); showToast("服务不可用"); })
    .finally(() => { btnRefresh.disabled = false; });
}

function setRange(id) {
  currentRange = id;
  relayout();
  draw();
  const tt = document.getElementById("tooltip");
  if (tt) tt.style.display = "none";
  if (wlPanelOpen) renderWatchlist();  // 自选面板区间联动
  if (view === "global" && gChart) renderGlobalMap();  // 地图区间联动
}

btnRefresh.addEventListener("click", () => refetchData());
const rangeSel = document.getElementById("rangeSel");
if (rangeSel) {
  rangeSel.addEventListener("change", e => setRange(e.target.value));
  // 双保险：部分自动化/边缘场景只触发 input 不触发 change
  rangeSel.addEventListener("input", e => setRange(e.target.value));
}
autoRefreshEl.addEventListener("change", () => {
  clearInterval(autoTimer);
  if (autoRefreshEl.checked) {
    autoTimer = setInterval(autoFetchTick, AUTO_REFRESH_MS);
    tickLive();
  } else {
    stopLive();
  }
});

// ---------- 自选股事件 ----------
const ttEl = document.getElementById("tooltip");
if (ttEl) ttEl.addEventListener("click", e => {
  const xb = e.target.closest(".tt-close");
  if (xb) {
    // 关闭按钮：收起并清 lastTip，防止行情 8s 刷新/缩放重绘把已关闭的浮层复活
    tooltip.style.display = "none";
    tooltipLocked = false;
    lastTip = null;
    return;
  }
  const db = e.target.closest("#ttDrillBtn");
  if (db) {
    // 下钻按钮：按行业名找板块节点（v90 三层树：root.children=一级，二级在其 children 里，两层搜索）
    const nm = db.getAttribute("data-name");
    let ind = null;
    if (root && root.children) {
      ind = root.children.find(x => x.name === nm) || null;
      if (!ind) {
        for (const sec of root.children) {
          const hit = sec.children && sec.children.find(x => x.name === nm);
          if (hit) { ind = hit; break; }
        }
      }
    }
    if (!ind) return;
    tooltip.style.display = "none";
    tooltipLocked = false;
    drillInto(ind);
    return;
  }
  const ab = e.target.closest("#ttAlertBtn");
  if (ab) {
    alertEditOpen(ab.getAttribute("data-code"), null, e.clientX, e.clientY);
    return;
  }
  const nb = e.target.closest(".tt-note-add, .tt-note-edit");
  if (nb) {
    const nc = nb.closest(".tt-note").getAttribute("data-code");
    const nn = (codeIndex && codeIndex.get(nc)) ? codeIndex.get(nc).name : nc;
    openNoteEditor(nc, nn, e.clientX, e.clientY);
    return;
  }
  const nd = e.target.closest(".tt-note-del");
  if (nd) {
    const nc = nd.closest(".tt-note").getAttribute("data-code");
    delete wlNotes[nc]; wlNotesSave();
    if (lastTip && lastTip.node && lastTip.node.code === nc) updateTooltip(lastTip.node, lastTip.mx, lastTip.my, lastTip.centered);
    draw(); showToast("备注已删除");
    return;
  }
  const b = e.target.closest("#ttWlBtn");
  if (!b) return;
  const code = b.getAttribute("data-code");
  const node = codeIndex ? codeIndex.get(code) : null;
  if (!node) return;
  if (wlFind(code)) { wlRemove(code); b.textContent = "☆ 加自选"; b.classList.remove("on"); }
  else { wlAdd(node); b.textContent = "★ 移出自选"; b.classList.add("on"); }
});
const btnWlEl = document.getElementById("btnWl");
if (btnWlEl) btnWlEl.addEventListener("click", wlToggle);
const wlCloseEl = document.getElementById("wlClose");
if (wlCloseEl) wlCloseEl.addEventListener("click", wlToggle);
const wlListEl = document.getElementById("wlList");
if (wlListEl) wlListEl.addEventListener("click", e => {
  const cbtn = e.target.closest(".wl-cost");
  if (cbtn) { e.stopPropagation(); costEditOpen(cbtn.getAttribute("data-code")); return; }
  const mon = e.target.closest(".wl-mon");
  if (mon) { e.stopPropagation(); toggleMonitor(mon.getAttribute("data-code")); return; }
  const del = e.target.closest(".wl-del");
  if (del) { e.stopPropagation(); wlRemove(del.getAttribute("data-code")); return; }
    const ghead = e.target.closest(".wl-ghead");
    if (ghead) {
      const grp = ghead.closest(".wl-group");
      const ind = grp ? grp.getAttribute("data-ind") : null;
      if (ind) {
        if (wlExpanded.has(ind)) wlExpanded.delete(ind); else wlExpanded.add(ind);
        wlSaveExpanded();
        grp.classList.toggle("collapsed");
      }
      return;
    }
  const row = e.target.closest(".wl-row");
  if (row) {
    const code = row.getAttribute("data-code");
    if (window.matchMedia && window.matchMedia("(max-width: 768px)").matches) wlMenuOpen(code);
    else window.open("https://xueqiu.com/S/" + xueqiuSymbol(code), "_blank");
  }
});
const wlClearBtnEl = document.getElementById("wlClearBtn");
if (wlClearBtnEl) wlClearBtnEl.addEventListener("click", () => {
  if (watchlist.length > 0 && confirm("清空全部自选股？")) wlClear();
});

// 右键点击个股格子 = 切换自选（屏蔽浏览器默认菜单）
cv.addEventListener("contextmenu", e => {
  e.preventDefault();
  const rect = cv.getBoundingClientRect();
  const node = nodeAtScreen(e.clientX - rect.left, e.clientY - rect.top);
  if (!node || !node.leaf) { showToast("请右键个股格子添加自选"); return; }
  toggleWatchlist(node);
});

// v20 Tab 切换 + 地图面板
document.getElementById("tabA").addEventListener("click", () => switchView("a"));
document.getElementById("tabGlobal").addEventListener("click", () => switchView("global"));
document.getElementById("tabFutures").addEventListener("click", () => switchView("futures"));
document.getElementById("tabCal").addEventListener("click", () => switchView("cal"));
const btnSpot = document.getElementById("btnSpot");
if (btnSpot) btnSpot.addEventListener("click", updateSpot);
const btnRe = document.getElementById("btnRe");
if (btnRe) btnRe.addEventListener("click", updateRe);
document.getElementById("cpClose").addEventListener("click", () => {
  document.getElementById("countryPanel").classList.add("hidden");
  gPanelOpen = false;
});
// v96 resize 防抖：地址栏动画/旋转会连发 resize，逐次重适配会让地图反复跳动
let gResizeTimer = null;
window.addEventListener("resize", () => {
  if (!gChart) return;
  clearTimeout(gResizeTimer);
  gResizeTimer = setTimeout(gRefitChart, 150);
});

// tooltip 锁定：鼠标移入浮层冻结位置，移出恢复跟随
const tipLockEl = document.getElementById("tooltip");
if (tipLockEl) {
  tipLockEl.addEventListener("pointerenter", () => { tooltipLocked = true; });
  tipLockEl.addEventListener("pointerleave", () => { tooltipLocked = false; });
}

// ===== 股票备注（localStorage 独立存储，任意股票可备注） =====
const noteEditorEl = document.getElementById("noteEditor");
const neTextEl = document.getElementById("neText");
const neTitleEl = document.getElementById("neTitle");
let noteEditingCode = null;
let lastTip = null;
function openNoteEditor(code, name, mx, my) {
  if (!noteEditorEl || !neTextEl) return;
  noteEditingCode = code;
  neTitleEl.textContent = name + "  " + code;
  neTextEl.value = noteGet(code);
  noteEditorEl.classList.remove("hidden");
  const ew = 260, eh = 172;
  let ex = (mx != null ? mx : W / 2) + 14, ey = (my != null ? my : H / 2) + 14;
  if (ex + ew > window.innerWidth - 8) ex = (mx != null ? mx : W / 2) - ew - 12;
  if (ey + eh > window.innerHeight - 8) ey = (my != null ? my : H / 2) - eh - 12;
  noteEditorEl.style.left = Math.max(4, ex) + "px";
  noteEditorEl.style.top = Math.max(4, ey) + "px";
  neTextEl.focus();
}
function closeNoteEditor() { noteEditingCode = null; if (noteEditorEl) noteEditorEl.classList.add("hidden"); }
function saveNote() {
  if (!noteEditingCode) return;
  const c = noteEditingCode;
  const t = neTextEl.value.trim();
  if (t) wlNotes[c] = t; else delete wlNotes[c];
  wlNotesSave();
  closeNoteEditor();
  draw();
  if (lastTip && lastTip.node) updateTooltip(lastTip.node, lastTip.mx, lastTip.my, lastTip.centered);
  showToast(t ? "备注已保存" : "备注已删除");
}
const neSaveEl = document.getElementById("neSave");
if (neSaveEl) neSaveEl.addEventListener("click", saveNote);
const neCloseEl = document.getElementById("neClose");
if (neCloseEl) neCloseEl.addEventListener("click", closeNoteEditor);
const neDelEl = document.getElementById("neDel");
if (neDelEl) neDelEl.addEventListener("click", () => {
  if (!noteEditingCode) return;
  const c = noteEditingCode;
  delete wlNotes[c]; wlNotesSave();
  closeNoteEditor(); draw();
  if (lastTip && lastTip.node) updateTooltip(lastTip.node, lastTip.mx, lastTip.my, lastTip.centered);
  showToast("备注已删除");
});

// ===== 自选列表悬停悬浮窗（显示行情 + 备注） =====
const wlTipEl = document.getElementById("wlTip");
let wlTipCode = null;
function showWlTip(row) {
  if (!wlTipEl) return;
  const code = row.getAttribute("data-code");
  if (!code) return;
  wlTipCode = code;
  const st = codeIndex ? codeIndex.get(code) : null;
  const nmEl = row.querySelector(".wl-name");
  const name = nmEl ? nmEl.textContent : (st ? st.name : code);
  const esc2 = s0 => String(s0).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const price = (st && st.price != null) ? fmtNum(st.price) : "—";
  const pc = st ? st.pct : null;
  const pctStr = (pc === null || pc === undefined || isNaN(pc)) ? "—" : ((pc > 0 ? "+" : "") + pc.toFixed(2) + "%");
  const rp = st ? rangePctOf(st) : null;
  const rpStr = rp === null ? "—" : ((rp > 0 ? "+" : "") + rp.toFixed(2) + "%");
  const rl = RANGES.find(r => r.id === currentRange).label;
  const note = noteGet(code);
  const cp2 = costPnl(code, st);
  let h = '<div style="font-weight:700;color:#fff;font-size:13px;margin-bottom:4px">' + esc2(name) + ' <span style="color:#7d8ba1;font-weight:400">' + code + "</span></div>";
  h += '<div class="wt-row"><span class="wt-label">现价</span><span>' + price + "</span></div>";
  h += '<div class="wt-row"><span class="wt-label">' + rl + '涨跌幅</span><span style="color:' + pctTextColor(rp) + ';font-weight:600">' + rpStr + "</span></div>";
  h += '<div class="wt-row"><span class="wt-label">当日涨跌幅</span><span style="color:' + pctTextColor(pc) + '">' + pctStr + "</span></div>";
  if (cp2) {
    const ccol = pctTextColor(cp2.pnlPct);
    const qtyStr = cp2.qty ? " · " + fmtQty(cp2.qty) + "股" : "";
    h += '<div class="wt-row"><span class="wt-label">成本价</span><span>' + fmtNum(cp2.cost) + qtyStr + "</span></div>";
    h += '<div class="wt-row"><span class="wt-label">盈亏额</span><span style="color:' + ccol + '">' + (cp2.pnlAmt != null ? ((cp2.pnlAmt > 0 ? "+" : "") + fmtNum(cp2.pnlAmt)) : "—") + "</span></div>";
    h += '<div class="wt-row"><span class="wt-label">盈亏率</span><span style="color:' + ccol + '">' + (cp2.pnlPct > 0 ? "+" : "") + cp2.pnlPct.toFixed(2) + "%</span></div>";
  }
  if (note) h += '<div class="wt-note">' + esc2(note) + "</div>";
  wlTipEl.innerHTML = h;
  wlTipEl.classList.remove("hidden");
  const rect = row.getBoundingClientRect();
  const tw = wlTipEl.offsetWidth, th = wlTipEl.offsetHeight;
  let tx = rect.left - tw - 8, ty = rect.top;
  if (tx < 8) tx = rect.right + 8;
  if (ty + th > window.innerHeight - 8) ty = window.innerHeight - th - 8;
  wlTipEl.style.left = tx + "px";
  wlTipEl.style.top = Math.max(8, ty) + "px";
}
function hideWlTip() { wlTipCode = null; if (wlTipEl) wlTipEl.classList.add("hidden"); }
if (wlListEl) {
  wlListEl.addEventListener("mouseover", e => {
    const row = e.target.closest(".wl-row");
    if (row) showWlTip(row);
  });
  wlListEl.addEventListener("mouseleave", () => hideWlTip());
}
if (wlTipEl) wlTipEl.addEventListener("mouseleave", () => hideWlTip());

// ===== 成本价编辑弹窗 v64 =====
const costEditorEl = document.getElementById("costEditor");
let costEditCode = null;
function costEditOpen(code) {
  costEditCode = code;
  const row = document.querySelector('.wl-row[data-code="' + code + '"]');
  const nm = row ? row.querySelector(".wl-name").textContent : "";
  const c = wlCosts ? wlCosts[code] : null;
  const ceCode = document.getElementById("ceCode");
  if (ceCode) ceCode.textContent = code + (nm ? " " + nm : "");
  const ceCost = document.getElementById("ceCost");
  if (ceCost) {
    if (c) {
      ceCost.value = String(c.cost);
    } else {
      // 未设置过成本价：默认填当前实时价（无价格则留空）
      const cst = codeIndex ? codeIndex.get(code) : null;
      const px = (cst && cst.price != null && isFinite(cst.price)) ? cst.price : null;
      ceCost.value = px !== null ? px.toFixed(2) : "";
    }
  }
  const ceQty = document.getElementById("ceQty");
  if (ceQty) ceQty.value = (c && c.qty) ? String(c.qty) : "";
  if (costEditorEl) costEditorEl.classList.remove("hidden");
}
function costEditClose() { if (costEditorEl) costEditorEl.classList.add("hidden"); costEditCode = null; }
if (costEditorEl) {
  const ceSave = document.getElementById("ceSave");
  if (ceSave) ceSave.addEventListener("click", () => {
    const code = costEditCode;
    const cv = parseFloat(document.getElementById("ceCost").value);
    const qv = document.getElementById("ceQty").value;
    const q = (qv !== "") ? parseFloat(qv) : null;
    if (code) costSet(code, cv, q);
    costEditClose();
  });
  const ceClear = document.getElementById("ceClear");
  if (ceClear) ceClear.addEventListener("click", () => { if (costEditCode) costSet(costEditCode, null, null); costEditClose(); });
  const ceCancel = document.getElementById("ceCancel");
  if (ceCancel) ceCancel.addEventListener("click", costEditClose);
}

// ===== 自选行操作菜单（移动端，v76） =====
const wlMenuEl = document.getElementById("wlMenu");
let wlMenuCode = null;
function wlMenuOpen(code) {
  wlMenuCode = code;
  const row = document.querySelector('.wl-row[data-code="' + code + '"]');
  const nm = row ? row.querySelector(".wl-name").textContent : "";
  const t = document.getElementById("wmTitle");
  if (t) t.textContent = nm + "  " + code;
  if (wlMenuEl) wlMenuEl.classList.remove("hidden");
}
function wlMenuClose() { wlMenuCode = null; if (wlMenuEl) wlMenuEl.classList.add("hidden"); }
if (wlMenuEl) {
  wlMenuEl.addEventListener("click", e => {
    const act = e.target.closest("[data-act]");
    if (!act) return;
    const code = wlMenuCode;
    const a = act.getAttribute("data-act");
    wlMenuClose();
    if (!code) return;
    if (a === "cost") costEditOpen(code);
    else if (a === "xq") window.open("https://xueqiu.com/S/" + xueqiuSymbol(code), "_blank");
    else if (a === "del") wlRemove(code);
  });
}

// ===== 告警系统（企业微信群推送，v49） =====
const alertPanelEl = document.getElementById("alertPanel");
const alertListEl = document.getElementById("alertList");
let alertPanelOpen = false;
let alertData = {};
let alertEditCode = null;
let alertEditIdx = null;
const alertTypeLabels = { price: "现价", pct: "当日涨跌幅", chg: "涨跌额", pct5: "近5日涨跌幅", pct10: "近10日涨跌幅", pct20: "近20日涨跌幅", pct60: "近60日涨跌幅", pctYTD: "年初至今涨跌幅" };
const alEsc = s0 => String(s0).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const alFindName = code => {
  if (codeIndex && codeIndex.get(code)) return codeIndex.get(code).name;
  const f = alFindFut(code);
  return f ? f.name : String(code);
};
const alFindFut = code => {
  const c = String(code).trim();
  const cl = c.toLowerCase();
  return fItems.find(it =>
    String(it.code).toLowerCase() === cl ||
    String(it.secid).toLowerCase() === cl ||
    (it.name && it.name.indexOf(c) >= 0)
  );
};
const alTypeOptions = (mkt, cur) => {
  const all = [
    ["price", "现价"], ["pct", "当日涨跌幅"], ["chg", "涨跌额"],
    ["pct5", "近5日涨跌幅"], ["pct10", "近10日涨跌幅"], ["pct20", "近20日涨跌幅"],
    ["pct60", "近60日涨跌幅"], ["pctYTD", "年初至今涨跌幅"]
  ];
  const allowed = mkt === "fut" ? ["price", "pct", "chg"] : all.map(x => x[0]);
  return all.filter(x => allowed.indexOf(x[0]) >= 0).map(x => {
    return '<option value="' + x[0] + '"' + (x[0] === cur ? " selected" : "") + ">" + x[1] + "</option>";
  }).join("");
};

function alertToggle() {
  alertPanelOpen = !alertPanelOpen;
  if (alertPanelEl) alertPanelEl.classList.toggle("hidden", !alertPanelOpen);
  if (alertPanelOpen) { alertLoad(); alertCfgLoad(); }
}
async function alertLoad() {
  if (!alertListEl) return;
  try {
    const res = await fetch("/api/alerts", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const j = await res.json();
    alertData = (j && j.alerts) || {};
    alertRender();
  } catch (e) {
    alertData = {};
    alertListEl.innerHTML = '<div class="al-err">告警接口不可用（/api/alerts）<br>请先重启服务（start.bat）再刷新</div>';
  }
}
async function alertCfgLoad() {
  try {
    const res = await fetch("/api/alertcfg", { cache: "no-store" });
    const j = await res.json();
    const c = (j && j.cfg) || {};
    const st = document.getElementById("awhStatus");
    if (st) st.textContent = c.hasKey ? ("已配置 " + c.webhook) : "未配置";
    const inp = document.getElementById("awhInput");
    if (inp) inp.placeholder = c.hasKey ? c.webhook : "企业微信群机器人 webhook 地址";
  } catch (e) {}
}
function awhSave() {
  const inp = document.getElementById("awhInput");
  const url = inp ? inp.value.trim() : "";
  if (!url) { showToast("请输入 webhook 地址"); return; }
  fetch("/api/alertcfg", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ webhook: url })
  }).then(r => r.json()).then(j => {
    if (j && j.ok) { showToast("股价告警 webhook 已保存"); if (inp) inp.value = ""; alertCfgLoad(); }
    else showToast("保存失败：" + ((j && j.msg) || "未知"));
  }).catch(() => showToast("服务不可用"));
}
const awhSaveEl = document.getElementById("awhSave");
if (awhSaveEl) awhSaveEl.addEventListener("click", awhSave);
const awhTestEl = document.getElementById("awhTest");
if (awhTestEl) awhTestEl.addEventListener("click", alertTestSend);
function alertSave() {
  return fetch("/api/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(alertData)
  }).then(r => r.json()).catch(() => ({ ok: false }));
}
function alertRender() {
  if (!alertListEl) return;
  const codes = Object.keys(alertData);
  const acEl = document.getElementById("alertCount");
  if (acEl) {
    let run = 0, total = 0;
    for (const c of codes) {
      const rs = alertData[c] || [];
      total += rs.length;
      run += rs.filter(r => r.enabled !== false).length;
    }
    acEl.textContent = run;
    const btn = document.getElementById("btnAlert");
    if (btn) btn.title = "股价告警（企业微信群推送）：" + run + " 条运行中 / " + total + " 条总数";
  }
  if (codes.length === 0) {
    alertListEl.innerHTML = '<div class="al-empty">暂无告警规则<br>悬停个股浮层点「🔔 告警」添加<br>或点下方「＋ 添加告警」</div>';
    return;
  }
  let html = "";
  for (const code of codes) {
    const rules = alertData[code] || [];
    const name = alFindName(code);
    for (let i = 0; i < rules.length; i++) {
      const r = rules[i];
      const off = !r.enabled;
      const stText = !r.enabled ? "已停用" : (r.state === "fired" ? "已触发" : "待触发");
      const stCls = !r.enabled ? "off" : (r.state === "fired" ? "fired" : "idle");
      const tLabel = alertTypeLabels[r.type] || r.type;
      const op = r.op === ">=" ? "≥" : "≤";
      const val = (r.type === "price" || r.type === "chg") ? Number(r.value).toFixed(2) : Number(r.value).toFixed(2) + "%";
      const firedTxt = r.firedAt ? new Date(r.firedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
      html += '<div class="al-item' + (off ? " off" : "") + '" data-code="' + code + '" data-idx="' + i + '">' +
        '<div class="al-top"><span class="al-name">' + alEsc(name) + '<span class="al-code">' + code + "</span></span>" +
        '<span class="al-st ' + stCls + '">' + stText + "</span></div>" +
        '<div class="al-cond">条件：' + tLabel + " " + op + " " + val + "</div>" +
        (r.note ? '<div class="al-note">' + alEsc(r.note) + "</div>" : "") +
        (firedTxt ? '<div class="al-time">触发于 ' + firedTxt + "</div>" : "") +
        '<div class="al-ops">' +
        '<button data-act="toggle">' + (off ? "启用" : "停用") + "</button>" +
        '<button data-act="reset">重置</button>' +
        '<button data-act="edit">编辑</button>' +
        '<button class="del" data-act="del">删除</button>' +
        "</div></div>";
    }
  }
  alertListEl.innerHTML = html;
}
if (alertListEl) alertListEl.addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const item = btn.closest(".al-item");
  if (!item) return;
  const code = item.getAttribute("data-code");
  const idx = Number(item.getAttribute("data-idx"));
  const act = btn.getAttribute("data-act");
  const rules = alertData[code] || [];
  const r = rules[idx];
  if (!r) return;
  if (act === "toggle") { r.enabled = !r.enabled; alertSave().then(() => alertRender()); }
  else if (act === "reset") { r.state = "idle"; r.resetAt = Date.now() / 1000; alertSave().then(() => alertRender()); showToast("已重置，可再次触发"); }
  else if (act === "edit") { alertEditOpen(code, idx); }
  else if (act === "del") {
    rules.splice(idx, 1);
    if (rules.length === 0) delete alertData[code];
    alertSave().then(() => alertRender());
  }
});
const alertCloseEl = document.getElementById("alertClose");
if (alertCloseEl) alertCloseEl.addEventListener("click", alertToggle);
const btnAlertEl = document.getElementById("btnAlert");
if (btnAlertEl) btnAlertEl.addEventListener("click", alertToggle);
const alertAddEl = document.getElementById("alertAdd");
if (alertAddEl) alertAddEl.addEventListener("click", () => alertEditOpen("", null));
async function alertTestSend() {
  try {
    const res = await fetch("/api/alerttest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ msg: "来自告警面板的测试" }) });
    const j = await res.json();
    showToast(j && j.ok ? "测试消息已推送" : ("推送失败：" + ((j && j.msg) || "未知")));
  } catch (e) { showToast("测试失败：接口不可用"); }
}
const alertTestEl = document.getElementById("alertTest");
if (alertTestEl) alertTestEl.addEventListener("click", alertTestSend);

// 编辑弹窗
const aeEditorEl = document.getElementById("alertEditor");
const aeTitleEl = document.getElementById("aeTitle");
const aeCodeEl = document.getElementById("aeCode");
const aeNameEl = document.getElementById("aeName");
const aeTypeEl = document.getElementById("aeType");
const aeOpEl = document.getElementById("aeOp");
const aeValueEl = document.getElementById("aeValue");
const aeNoteEl = document.getElementById("aeNote");
const aeAutoOffEl = document.getElementById("aeAutoOff");
const aeErrEl = document.getElementById("aeErr");
const aeDelEl = document.getElementById("aeDel");
function alertEditMktChange() {
  const mkt = aeMktEl ? aeMktEl.value : "stock";
  if (aeCodeEl) aeCodeEl.placeholder = mkt === "fut" ? "期货代码，如 rbm（螺纹钢）" : "代码：A股 6 位数字，如 600519";
  if (aeNameEl) aeNameEl.textContent = "";
  if (aeErrEl) aeErrEl.textContent = "";
  if (aeTypeEl) aeTypeEl.innerHTML = alTypeOptions(mkt, mkt === "fut" ? "price" : "price");
}
function alertEditOpen(code, idx, mx, my) {
  alertEditCode = code || "";
  alertEditIdx = idx;
  if (aeCodeEl) aeCodeEl.value = alertEditCode;
  if (aeNameEl) aeNameEl.textContent = "";
  if (aeErrEl) aeErrEl.textContent = "";
  if (idx === null || idx === undefined) {
    if (aeTitleEl) aeTitleEl.textContent = "添加告警";
    if (aeMktEl) aeMktEl.value = "stock";
    alertEditMktChange();
    if (aeTypeEl) aeTypeEl.value = "price";
    if (aeOpEl) aeOpEl.value = ">=";
    if (aeValueEl) aeValueEl.value = "";
    if (aeNoteEl) aeNoteEl.value = "";
    if (aeAutoOffEl) aeAutoOffEl.checked = false;
    if (aeDelEl) aeDelEl.classList.add("hidden");
  } else {
    const r = ((alertData[alertEditCode] || [])[idx]) || {};
    if (aeTitleEl) aeTitleEl.textContent = "编辑告警";
    if (aeMktEl) aeMktEl.value = r.mkt === "fut" ? "fut" : "stock";
    alertEditMktChange();
    if (aeTypeEl) aeTypeEl.value = r.type || "price";
    if (aeOpEl) aeOpEl.value = r.op || ">=";
    if (aeValueEl) aeValueEl.value = r.value;
    if (aeNoteEl) aeNoteEl.value = r.note || "";
    if (aeAutoOffEl) aeAutoOffEl.checked = !!r.autoOff;
    if (aeDelEl) aeDelEl.classList.remove("hidden");
    const nm = alFindName(alertEditCode);
    if (aeNameEl && nm) aeNameEl.textContent = nm + " " + alertEditCode;
  }
  if (aeEditorEl) {
    aeEditorEl.classList.remove("hidden");
    const ew = 270, eh = 280;
    let ex = (mx != null ? mx : W / 2) + 14, ey = (my != null ? my : H / 2) + 14;
    if (ex + ew > window.innerWidth - 8) ex = (mx != null ? mx : W / 2) - ew - 12;
    if (ey + eh > window.innerHeight - 8) ey = (my != null ? my : H / 2) - eh - 12;
    aeEditorEl.style.left = Math.max(4, ex) + "px";
    aeEditorEl.style.top = Math.max(4, ey) + "px";
  }
  if (aeCodeEl) aeCodeEl.focus();
}
function alertEditClose() { if (aeEditorEl) aeEditorEl.classList.add("hidden"); }
// 告警弹窗可拖动（头部为手柄，关闭按钮除外）
(function () {
  const ed = document.getElementById("alertEditor");
  if (!ed) return;
  const head = ed.querySelector(".ne-head");
  if (!head) return;
  let drag = null;
  head.addEventListener("mousedown", e => {
    if (e.target.closest(".ne-close")) return;
    const r = ed.getBoundingClientRect();
    drag = { sx: e.clientX, sy: e.clientY, ox: r.left, oy: r.top };
    e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!drag) return;
    const nx = Math.max(4, drag.ox + e.clientX - drag.sx);
    const ny = Math.max(4, drag.oy + e.clientY - drag.sy);
    ed.style.left = nx + "px";
    ed.style.top = ny + "px";
  });
  document.addEventListener("mouseup", () => { drag = null; });
})();
function alertEditSave() {
  let code = (aeCodeEl ? aeCodeEl.value : "").trim();
  const mkt = aeMktEl ? aeMktEl.value : "stock";
  let secid = "";
  if (mkt === "fut") {
    const f = alFindFut(code);
    if (!f) { if (aeErrEl) aeErrEl.textContent = "未找到该期货品种（如 rbm/im/cum）"; return; }
    code = f.code;   // 规范化：alerts key 用条目标准 code，与服务端 f12 匹配
    secid = f.secid;
  } else {
    if (!/^\d{6}$/.test(code)) { if (aeErrEl) aeErrEl.textContent = "请输入 6 位股票代码"; return; }
    const st = codeIndex ? codeIndex.get(code) : null;
    if (!st) { if (aeErrEl) aeErrEl.textContent = "未找到该股票，请确认代码正确"; return; }
    secid = (code.charAt(0) === "6" ? "1." : "0.") + code;
  }
  const value = parseFloat(aeValueEl ? aeValueEl.value : "");
  if (isNaN(value)) { if (aeErrEl) aeErrEl.textContent = "请输入有效阈值"; return; }
  const ttype = aeTypeEl ? aeTypeEl.value : "price";
  const old = (alertEditIdx === null || alertEditIdx === undefined) ? null : ((alertData[code] || [])[alertEditIdx] || null);
  const rule = {
    id: old ? old.id : ("a" + Date.now()),
    mkt: mkt,
    secid: secid,
    type: ttype,
    op: aeOpEl ? aeOpEl.value : ">=",
    value: value,
    note: aeNoteEl ? aeNoteEl.value.trim() : "",
    enabled: true,
    autoOff: !!(aeAutoOffEl && aeAutoOffEl.checked),
    state: old ? (old.state || "idle") : "idle",
    firedAt: old ? old.firedAt : undefined,
    resetAt: old ? old.resetAt : 0,
    lastErr: old ? old.lastErr : undefined
  };
  if (!old) {
    if (!alertData[code]) alertData[code] = [];
    alertData[code].push(rule);
  } else {
    alertData[code][alertEditIdx] = rule;
  }
  alertEditClose();
  alertSave().then(j => {
    if (j && j.ok) { alertLoad(); showToast("告警已保存"); }
    else showToast("保存失败，请检查服务");
  });
}
const aeSaveEl = document.getElementById("aeSave");
if (aeSaveEl) aeSaveEl.addEventListener("click", alertEditSave);
const aeCancelEl = document.getElementById("aeCancel");
if (aeCancelEl) aeCancelEl.addEventListener("click", alertEditClose);
const aeCloseEl = document.getElementById("aeClose");
if (aeCloseEl) aeCloseEl.addEventListener("click", alertEditClose);
if (aeDelEl) aeDelEl.addEventListener("click", () => {
  const rules = alertData[alertEditCode] || [];
  rules.splice(alertEditIdx, 1);
  if (rules.length === 0) delete alertData[alertEditCode];
  alertEditClose();
  alertSave().then(() => { alertLoad(); showToast("告警已删除"); });
});
if (aeCodeEl) aeCodeEl.addEventListener("input", () => {
  const code = aeCodeEl.value.trim();
  const mkt = aeMktEl ? aeMktEl.value : "stock";
  if (mkt === "fut") {
    const f = alFindFut(code);
    if (aeNameEl) aeNameEl.textContent = f ? (f.name + " (" + f.code + ")") : "";
  } else {
    const st = codeIndex ? codeIndex.get(code) : null;
    if (aeNameEl) aeNameEl.textContent = st ? (st.name + " " + code) : "";
  }
  if (aeErrEl) aeErrEl.textContent = "";
});
const aeMktEl = document.getElementById("aeMkt");
if (aeMktEl) aeMktEl.addEventListener("change", alertEditMktChange);

// 初始化告警数量（页面加载即显示，badge 实时同步）
alertLoad();
// 初始化自选（本地存储）
wlLoad();
wlNotesLoad();
monitorLoad();
  costLoad();
renderWatchlist();

// 加载全球指数数据（不阻塞 A股主流程）
loadGlobalData();

// 加载期货数据（不阻塞 A股主流程）
loadFuturesData();

// ---------- 启动 ----------
function sizeCanvas() {
  const wrap = document.getElementById("wrap");
  const w = wrap.clientWidth;
  const hh = wrap.clientHeight;
  // wrap 隐藏（切到其他 Tab 时 display:none）→ clientWidth/Height 为 0，跳过以免把画布清空
  if (w <= 0 || hh <= 0) return;
  W = w;
  H = hh;
  WORLD_W = (H > W * 1.05) ? (W / H) : 1;   // 竖屏（明显高>宽）：世界宽=W/H<1 → 宽满屏；横屏保持 1x1
  dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr;
  cv.height = H * dpr;
  cv.style.width = W + "px";
  cv.style.height = H + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function init() {
  sizeCanvas();
  baseScale = (WORLD_W < 1 ? H : Math.min(W, H)) * 0.98;   // 竖屏：世界高 1 → scale=H；横屏不变
  resetView();
  fetchData(false);
  autoTimer = setInterval(autoFetchTick, AUTO_REFRESH_MS);
  // 实时行情：交易时段每 8 秒直连东财刷新价格（可用 ?live=1 强制开启，供调试/复盘）
  setInterval(tickLive, 30000);
  tickLive();

  // 现货模块：加载一次（切到期货 Tab 时再读一次，更新按钮即时刷新）
  startSpotLive();
  startReLive();
}

init();

// ===== v72 移动端「更多」菜单 =====
(function () {
  const btnMore = document.getElementById("btnMore");
  const moreMenu = document.getElementById("moreMenu");
  if (!btnMore || !moreMenu) return;
  const syncMoreMenu = () => {
    moreMenu.querySelectorAll(".mm-item[data-tab]").forEach(el => el.classList.toggle("on", el.getAttribute("data-tab") === view));
    const ar2 = document.getElementById("autoRefresh2");
    if (ar2) ar2.checked = autoRefreshEl.checked;
  };
  btnMore.addEventListener("click", e => { e.stopPropagation(); moreMenu.classList.toggle("hidden"); if (!moreMenu.classList.contains("hidden")) syncMoreMenu(); });
  document.addEventListener("click", e => { if (!moreMenu.classList.contains("hidden") && e.target !== btnMore && !moreMenu.contains(e.target)) moreMenu.classList.add("hidden"); });
  moreMenu.addEventListener("click", e => {
    const it = e.target.closest(".mm-item");
    if (!it) return;
    if (it.hasAttribute("data-tab")) { switchView(it.getAttribute("data-tab")); moreMenu.classList.add("hidden"); }
    else if (it.id === "mmRefresh") { refetchData(); moreMenu.classList.add("hidden"); }
  });
  const ar2 = document.getElementById("autoRefresh2");
  if (ar2) ar2.addEventListener("change", () => { autoRefreshEl.checked = ar2.checked; autoRefreshEl.dispatchEvent(new Event("change")); });
})();

// ===== v73 触屏适配：双指捏合缩放 =====
let touchPinching = false, pinchDist0 = 0, pinchScale0 = 1, pinchEndAt = 0;
cv.addEventListener("touchstart", e => {
  if (e.touches.length === 2) {
    touchPinching = true;
    dragging = false;
    cv.classList.remove("dragging");
    pinchDist0 = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    pinchScale0 = scale;
  }
}, { passive: true });
cv.addEventListener("touchmove", e => {
  if (touchPinching && e.touches.length === 2) {
    e.preventDefault();
    const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    const rect = cv.getBoundingClientRect();
    const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
    const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
    const ns = clamp(pinchScale0 * (d / Math.max(pinchDist0, 1)), baseScale * 0.5, MAX_SCALE);
    const wx = (mx - offsetX) / scale, wy = (my - offsetY) / scale;
    offsetX = mx - wx * ns; offsetY = my - wy * ns;
    scale = ns;
    draw();
  }
}, { passive: false });
const endPinch = () => { if (touchPinching) { touchPinching = false; pinchEndAt = performance.now() + 600; } };
cv.addEventListener("touchend", endPinch);
cv.addEventListener("touchcancel", endPinch);


// ---------- 财经日历（v86：数据=服务端每5分钟抓取的 calendar.json，前端按需刷新） ----------
let calItems = null;
let calImp = "all", calType = "all";
let calFetchedAt = "";
let calStatus = null;  // {last_ok, last_fail}
let calExpandedDays = null;   // 折叠态：null=未初始化（按默认规则），Set<date>=手动模式的展开日（首次点击时继承当前展开状态）
const calExpanded = new Set();
let calQuery = "";            // 标题搜索词（空=未搜索）；重渲染自动继承
let calSearchTimer = 0;

function calApplySearch(q, xEl) {
  calQuery = String(q || "").trim();
  if (xEl) xEl.classList.toggle("show", !!calQuery);
  renderCalendar();
}
function calSearchSchedule(input, xEl) {
  clearTimeout(calSearchTimer);
  calSearchTimer = setTimeout(() => calApplySearch(input.value, xEl), 200);
}

function calEsc(s) { return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function calNum(x) { const n = parseFloat(x); return isNaN(n) ? null : n; }
function calTomorrowStr() { const d = new Date(); d.setDate(d.getDate() + 1); return d.toLocaleDateString("sv"); }
function calNextMondayStr() { const d = new Date(); const add = (8 - d.getDay()) % 7 || 7; d.setDate(d.getDate() + add); return d.toLocaleDateString("sv"); }

function fetchCalendar() {
  fetch("calendar.json?t=" + Date.now(), { cache: "no-store" })
    .then(r => r.json())
    .then(j => {
      calItems = (j && j.items) || [];
      calFetchedAt = (j && j.fetched_at) || "";
      renderCalendar();
      calUpdateDot();
      if (view === "cal") calMarkSeen();  // 数据到位后再标记已读（修复切换瞬间数据未到的时序）
    })
    .catch(() => { if (!calItems) renderCalendar(); });  // 失败保留旧数据；首失败显示空态
  // 抓取状态：失败时在「抓取于」区直观显示
  fetch("calstatus.json?t=" + Date.now(), { cache: "no-store" })
    .then(r => r.json())
    .then(s => {
      calStatus = s || null;
      if (view === "cal") renderCalendar();
    })
    .catch(() => {});
}

function calCountdown(ts, nowTs) {
  const d = ts - nowTs;
  if (d <= 0) return "";
  if (d < 3600) return Math.max(1, Math.round(d / 60)) + " 分后";
  if (d < 86400) return Math.round(d / 3600) + " 小时后";
  return Math.round(d / 86400) + " 天后";
}

function calBadge(it, nowTs, todayStr) {
  const hasA = it.actual !== "" && it.actual !== null && it.actual !== undefined;
  if (it.ts <= nowTs) {
    if (it.type === "FD" || !hasA) return '<span class="cal-badge">已过</span>';
    const a = calNum(it.actual), f = calNum(it.fcst);
    if (a !== null && f !== null) {
      if (a > f) return '<span class="cal-badge">高于预期</span>';
      if (a < f) return '<span class="cal-badge">低于预期</span>';
      return '<span class="cal-badge">符合预期</span>';
    }
    return '<span class="cal-badge">已公布</span>';
  }
  const cd = calCountdown(it.ts, nowTs);
  if (it.ts - nowTs <= 3600) return '<span class="cal-badge soon">' + cd + "</span>";
  if (it.date === todayStr) return '<span class="cal-badge">今晚</span>';
  return '<span class="cal-badge">待公布</span>';
}

function calActClass(it) {
  const a = calNum(it.actual), f = calNum(it.fcst);
  if (a === null || f === null) return "";
  if (a > f) return " hi";
  if (a < f) return " lo";
  return " eq";
}

// 标题高亮：先整体转义，再按大小写不敏感索引切三段注入 <mark>（外部数据，禁止先注入后转义）
function calTitleHtml(t) {
  const s = String(t || "");
  const q = calQuery.toLowerCase();
  if (!q) return calEsc(s);
  const i = s.toLowerCase().indexOf(q);
  if (i < 0) return calEsc(s);
  return calEsc(s.slice(0, i)) + "<mark>" + calEsc(s.slice(i, i + q.length)) + "</mark>" + calEsc(s.slice(i + q.length));
}

function calRow(it, nowTs, todayStr) {
  const dim = it.ts <= nowTs - 7200 ? " dim" : "";
  const open = calExpanded.has(String(it.id));
  const actCls = calActClass(it);
  return '<button class="cal-ev' + dim + '" type="button" data-id="' + it.id + '" aria-expanded="' + (open ? "true" : "false") + '">' +
    '<span class="t">' + it.t + '</span>' +
    '<span class="imp ' + (it.imp >= 3 ? "cal-star3" : "cal-star2") + '">' + it.imp + '\u2605</span>' +
    '<span class="cc">' + calEsc(it.country || it.cc) + '</span>' +
    '<span class="ttl"><span class="ttlt">' + calTitleHtml(it.title) + '</span>' + (it.is_new ? '<b class="ntag">新</b>' : "") + (it.upgraded ? '<b class="ntag up">升</b>' : "") + '</span>' +
    '<span class="cal-vals"><span class="v"><i>前值</i>' + calEsc(it.prev || "—") + '</span><span class="v"><i>预期</i>' + calEsc(it.fcst || "—") + '</span><span class="v' + actCls + '"><i>公布值</i>' + calEsc(it.actual || "—") + '</span></span>' +
    calBadge(it, nowTs, todayStr) + '</button>' +
    '<div class="cal-detail' + (open ? " open" : "") + '"><div class="row"><b>说明：</b>' + (it.foresight ? calEsc(it.foresight) : "暂无前瞻说明") + '</div>' +
    (it.period ? '<div class="row"><b>期次：</b>' + calEsc(it.period) + '</div>' : "") +
    '<div class="row"><b>类型：</b>' + (it.type === "FD" ? "财经事件" : "经济数据") + ' · ' + calEsc(it.country) + '</div></div>';
}

function calFocusCard(it, nowTs) {
  const soon = it.ts > nowTs && it.ts - nowTs <= 3600;
  const hasA = it.actual !== "" && it.actual !== null && it.actual !== undefined;
  let status;
  if (it.ts <= nowTs) {
    if (it.type === "FD" || !hasA) status = '<span class="cal-fx-status">已过</span>';
    else {
      const a = calNum(it.actual), f = calNum(it.fcst);
      let tag = "公布 " + it.actual;
      if (a !== null && f !== null) tag += a > f ? " · 高于预期" : (a < f ? " · 低于预期" : " · 符合预期");
      status = '<span class="cal-fx-status">' + tag + '</span>';
    }
  } else {
    status = '<span class="cal-fx-status' + (soon ? " soon" : "") + '">' + calCountdown(it.ts, nowTs) + '</span>';
  }
  return '<div class="cal-fcard' + (soon ? " soon" : "") + '">' +
    '<div class="cal-fx-top"><span class="cal-fx-time">' + it.t + '</span><span class="cal-fx-cc">' + calEsc(it.country) + ' · ' + (it.type === "FD" ? "事件" : "数据") + '</span></div>' +
    '<div class="cal-fx-title">' + calEsc(it.title) + ' <span class="' + (it.imp >= 3 ? "cal-star3" : "cal-star2") + '">' + it.imp + '\u2605</span></div>' +
    status + '</div>';
}

function calDayHtml(date, items, nowTs, todayStr, tomorrowStr) {
  const d = new Date(date + "T12:00:00");
  const wd = "周" + "日一二三四五六"[d.getDay()];
  const label = date === todayStr ? "今天" : (date === tomorrowStr ? "明天" : "");
  const s3 = items.filter(x => x.imp >= 3).length;
  const nn = items.filter(x => x.is_new).length;
  // 折叠：默认今天/明天/有新增展开；用户手动切换后按 calExpandedDays；
  // 搜索期强制展开——只读覆盖，不写 calExpandedDays，清空后恢复用户折叠态
  let expanded;
  if (calQuery) {
    expanded = true;
  } else if (calExpandedDays === null) {
    expanded = (date === todayStr || date === tomorrowStr || nn > 0);
  } else {
    expanded = calExpandedDays.has(date);
  }
  return '<section class="cal-day' + (expanded ? "" : " cal-col") + '" id="calday-' + date + '">' +
    '<div class="cal-day-head" role="button" tabindex="0" data-day="' + date + '" aria-expanded="' + (expanded ? "true" : "false") + '">' +
    '<span class="cal-day-arrow">' + (expanded ? "\u25BE" : "\u25B8") + '</span>' +
    '<span class="d-date">' + parseInt(date.slice(5, 7), 10) + '月' + parseInt(date.slice(8), 10) + '日</span>' +
    '<span class="d-week">' + wd + (label ? " · " + label : "") + '</span>' +
    '<span class="d-meta">' + items.length + ' 条 · 三星 <b>' + s3 + '</b>' + (nn ? ' · <b class="n-ct">新增 ' + nn + '</b>' : "") + '</span></div>' +
    '<div class="cal-day-body"' + (expanded ? "" : ' style="display:none"') + '>' + items.map(it => calRow(it, nowTs, todayStr)).join("") + '</div></section>';
}

function renderCalendar() {
  const list = document.getElementById("calList");
  const focus = document.getElementById("calFocus");
  const focusTitle = document.getElementById("calFocusTitle");
  const stamp = document.getElementById("calStamp");
  if (!list) return;
  if (stamp) {
    const nNew = (calItems || []).filter(x => x.is_new).length;
    if (calStatus && calStatus.last_fail && (!calStatus.last_ok || calStatus.last_fail.ts > calStatus.last_ok.ts)) {
      // 失败态：只显示失败，省略「抓取于」前缀（上次成功时间无信息价值）
      const f = calStatus.last_fail;
      stamp.innerHTML = '<b class="cal-fail" title="' + calEsc((f.err || "").slice(0, 60)) + '">抓取失败 ' + (f.at || "").slice(11, 16) + '</b>';
      return;
    }
    stamp.innerHTML = calFetchedAt
      ? ("抓取于 <b>" + calFetchedAt.slice(11, 16) + "</b>" + (nNew ? ' · <b class="new-count">' + nNew + ' 新</b>' : ""))
      : "尚无数据";
  }
  if (!calItems || !calItems.length) {
    list.innerHTML = '<div class="cal-empty">未查询到日历数据（服务端每 5 分钟自动重试）</div>';
    focus.innerHTML = ""; focusTitle.style.display = "none";
    return;
  }
  const nowTs = Date.now() / 1000;
  const todayStr = new Date().toLocaleDateString("sv");
  const tomorrowStr = calTomorrowStr();
  // 过滤 → 分组（星级/类型 AND 标题搜索，大小写不敏感子串）
  const q = calQuery.toLowerCase();
  const vis = calItems.filter(it =>
    (calImp === "all" || it.imp >= Number(calImp)) &&
    (calType === "all" || it.type === calType) &&
    (!q || String(it.title || "").toLowerCase().includes(q)));
  const groups = {};
  for (const it of vis) (groups[it.date] = groups[it.date] || []).push(it);
  const dates = Object.keys(groups).sort();
  // 焦点区：搜索期间隐藏卡片，标题行复用为匹配计数
  if (q) {
    focusTitle.style.display = "";
    focusTitle.textContent = "搜索 “" + calQuery + "” · " + vis.length + " 条匹配 · " + dates.length + " 天";
    focus.innerHTML = "";
  } else {
    // 今日重点：三星+，今天，最多 10 张
    const focusItems = calItems.filter(it => it.imp >= 3 && it.date === todayStr).slice(0, 10);
    if (focusItems.length) {
      focusTitle.style.display = "";
      focusTitle.textContent = "今日重点（三星及以上）";
      focus.innerHTML = focusItems.map(it => calFocusCard(it, nowTs)).join("");
    } else {
      focusTitle.style.display = "none";
      focus.innerHTML = "";
    }
  }
  if (!dates.length) {
    list.innerHTML = q
      ? '<div class="cal-empty">无匹配 “' + calEsc(calQuery) + '” 的事件</div>'
      : '<div class="cal-empty">当前过滤条件下无事件</div>';
    return;
  }
  list.innerHTML = dates.map(d => calDayHtml(d, groups[d], nowTs, todayStr, tomorrowStr)).join("");
}

function calUpdateDot() {
  const dot = document.getElementById("calDot");
  if (!dot) return;
  const newIds = (calItems || []).filter(x => x.is_new).map(x => String(x.id));
  let seen = [];
  try { seen = JSON.parse(localStorage.getItem("cal_seen_new_ids") || "[]"); } catch (e) { seen = []; }
  const seenSet = new Set(seen);
  const unseen = newIds.filter(id => !seenSet.has(id));
  dot.style.display = unseen.length ? "inline-block" : "none";
  dot.title = unseen.length ? unseen.length + " 条未读新事件" : "";
}
function calMarkSeen() {
  const newIds = (calItems || []).filter(x => x.is_new).map(x => String(x.id));
  if (newIds.length) {
    try { localStorage.setItem("cal_seen_new_ids", JSON.stringify(newIds)); } catch (e) {}
  }
  calUpdateDot();
}

(function initCalendar() {
  const bar = document.getElementById("calBar");
  if (!bar) return;
  const calList = document.getElementById("calList");
  if (calList) {
    calList.addEventListener("click", e => {
      const head = e.target.closest(".cal-day-head");
      if (!head) return;
      const date = head.getAttribute("data-day");
      if (!date) return;
      if (calExpandedDays === null) {
        // 首次手动切换：继承当前实际展开的天（今天/明天/有新增），再叠加本次操作。
        // 否则整表切入手动模式后，其余天会在下次重建（30s tick/数据刷新）时集体折叠
        calExpandedDays = new Set(
          Array.from(document.querySelectorAll("#calList .cal-day:not(.cal-col)"))
            .map(el => el.id.slice("calday-".length)).filter(Boolean));
      }
      if (calExpandedDays.has(date)) calExpandedDays.delete(date); else calExpandedDays.add(date);
      const day = document.getElementById("calday-" + date);
      if (!day) return;
      const open = calExpandedDays.has(date);
      day.classList.toggle("cal-col", !open);
      head.setAttribute("aria-expanded", open ? "true" : "false");
      const arrow = head.querySelector(".cal-day-arrow");
      if (arrow) arrow.textContent = open ? "\u25BE" : "\u25B8";
      const body = day.querySelector(".cal-day-body");
      if (body) body.style.display = open ? "" : "none";
    });
    calList.addEventListener("keydown", e => {
      if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("cal-day-head")) {
        e.preventDefault(); e.target.click();
      }
    });
  }
  // 标题搜索：IME 组词期跳过过滤（拼音字母不触发），compositionend 后统一应用
  const sInput = document.getElementById("calSearch");
  const sX = document.getElementById("calSearchX");
  if (sInput) {
    let composing = false;
    sInput.addEventListener("compositionstart", () => { composing = true; });
    sInput.addEventListener("compositionend", () => { composing = false; calSearchSchedule(sInput, sX); });
    sInput.addEventListener("input", () => { if (!composing) calSearchSchedule(sInput, sX); });
    sInput.addEventListener("keydown", e => {
      if (e.key === "Escape" && sInput.value) { sInput.value = ""; calApplySearch("", sX); sInput.blur(); }
    });
    if (sX) sX.addEventListener("click", () => { sInput.value = ""; calApplySearch("", sX); sInput.focus(); });
  }
  bar.addEventListener("click", e => {
    const p = e.target.closest(".cal-pill");
    if (p) {
      p.parentElement.querySelectorAll(".cal-pill").forEach(x => x.classList.remove("on"));
      p.classList.add("on");
      if (p.dataset.f === "imp") calImp = p.dataset.v; else calType = p.dataset.v;
      renderCalendar();
      return;
    }
    const a = e.target.closest(".cal-anchor");
    if (a) {
      const target = a.dataset.date === "today" ? new Date().toLocaleDateString("sv")
        : a.dataset.date === "week" ? calTomorrowStr() : calNextMondayStr();
      const el = document.getElementById("calday-" + target);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      else showToast("该日期范围暂无事件");
    }
  });
  const refreshBtn = document.getElementById("calRefresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    if (refreshBtn.disabled) return;
    refreshBtn.disabled = true;
    const oldHtml = refreshBtn.innerHTML;
    refreshBtn.innerHTML = "抓取中…";
    fetch("/api/cal_refetch", { method: "POST", cache: "no-store" })
      .then(r => r.json())
      .then(j => {
        if (j && j.ok) { fetchCalendar(); if (j.fresh) showToast("数据已是最新"); }
        else showToast((j && j.msg) || "抓取失败");
      })
      .catch(() => showToast("服务不可用"))
      .finally(() => { refreshBtn.disabled = false; refreshBtn.innerHTML = oldHtml; });
  });
  const listEl = document.getElementById("calList");
  listEl.addEventListener("click", e => {
    const row = e.target.closest(".cal-ev");
    if (!row) return;
    const det = row.nextElementSibling;
    if (det && det.classList.contains("cal-detail")) {
      const open = det.classList.toggle("open");
      row.setAttribute("aria-expanded", open ? "true" : "false");
      const id = String(row.dataset.id);
      if (open) calExpanded.add(id); else calExpanded.delete(id);
    }
  });
  // 倒计时每 30 秒重渲染（保持激活视图时）；数据每 120 秒跟随服务端刷新
  setInterval(() => { if (view === "cal") renderCalendar(); }, 30000);
  setInterval(() => { if (view === "cal") fetchCalendar(); }, 120000);
})();
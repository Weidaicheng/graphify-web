/* graph_viewer.js — D3.js force-directed graph viewer */

let svgEl, g, simulation, zoomBehavior;
let nodesData = [], linksData = [];
let nodeElements, linkElements, labelElements;
let selectedNode = null;
let width = 800, height = 600;

const colorScale = d3.scaleOrdinal(d3.schemeTableau10);

// ── Init ───────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('graph-container');
    const rect = container.getBoundingClientRect();
    width = rect.width || 800;
    height = rect.height || 600;

    initSVG();
    loadGraphData();

    // Resize observer for responsive layout
    new ResizeObserver(() => {
        const r = container.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            width = r.width;
            height = r.height;
            svgEl.attr('width', width).attr('height', height);
            if (simulation) {
                simulation.force('center', d3.forceCenter(width / 2, height / 2));
                simulation.force('x', d3.forceX(width / 2).strength(0.03));
                simulation.force('y', d3.forceY(height / 2).strength(0.03));
                simulation.alpha(0.3).restart();
            }
        }
    }).observe(container);
});

function initSVG() {
    svgEl = d3.select('#graph-svg')
        .attr('width', width)
        .attr('height', height);

    zoomBehavior = d3.zoom()
        .scaleExtent([0.05, 10])
        .on('zoom', (event) => {
            g.attr('transform', event.transform);
            const k = event.transform.k;
            g.selectAll('.node text')
                .attr('display', function(d) { return k > 1.2 || d.degree > 10 ? null : 'none'; });
        });

    svgEl.call(zoomBehavior);
    g = svgEl.append('g');
}

// ── Data loading ───────────────────────────────────────────

async function loadGraphData() {
    const params = new URLSearchParams();
    params.set('max_nodes', document.getElementById('max-nodes')?.value || 500);

    const commFilter = document.getElementById('community-filter')?.value;
    if (commFilter) params.set('community', commFilter);

    const ftFilter = document.getElementById('filetype-filter')?.value;
    if (ftFilter) params.set('file_type', ftFilter);

    try {
        const data = await apiFetch(`/api/repos/${REPO_OWNER}/${REPO_NAME}/d3-data?${params}`);
        nodesData = data.nodes;
        linksData = data.links;
        render();
    } catch (e) {
        console.error('Failed to load graph data:', e);
    }

    document.getElementById('graph-loading')?.classList.add('hidden');
}

// ── Rendering ──────────────────────────────────────────────

function render() {
    g.selectAll('*').remove();

    // Links
    linkElements = g.append('g')
        .selectAll('line')
        .data(linksData)
        .join('line')
        .attr('class', 'link');

    // Nodes
    const nodeGroup = g.append('g')
        .selectAll('g')
        .data(nodesData)
        .join('g')
        .attr('class', 'node')
        .call(d3.drag()
            .on('start', dragStart)
            .on('drag', dragging)
            .on('end', dragEnd))
        .on('click', (event, d) => onNodeClick(d))
        .on('mouseover', (event, d) => showTooltip(event, d))
        .on('mouseout', hideTooltip);

    nodeElements = nodeGroup.append('circle')
        .attr('r', d => Math.max(3, Math.min(12, 2 + Math.sqrt(d.degree) * 1.2)))
        .attr('fill', d => colorScale(d.community));

    labelElements = nodeGroup.append('text')
        .text(d => d.label.length > 20 ? d.label.slice(0, 18) + '…' : d.label)
        .attr('dx', d => Math.max(3, Math.min(12, 2 + Math.sqrt(d.degree) * 1.2)) + 4)
        .attr('dy', 3)
        .attr('display', d => d.degree > 10 ? null : 'none');

    // Force simulation
    if (simulation) simulation.stop();

    simulation = d3.forceSimulation(nodesData)
        .force('link', d3.forceLink(linksData).id(d => d.id).distance(40).strength(0.3))
        .force('charge', d3.forceManyBody().strength(-60).distanceMax(400))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(d => Math.max(4, Math.min(14, 2 + Math.sqrt(d.degree) * 1.2)) + 2))
        .force('x', d3.forceX(width / 2).strength(0.03))
        .force('y', d3.forceY(height / 2).strength(0.03))
        .alphaDecay(0.02)
        .on('tick', ticked);
}

function ticked() {
    linkElements
        .attr('x1', d => d.source.x)
        .attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x)
        .attr('y2', d => d.target.y);

    g.selectAll('.node')
        .attr('transform', d => `translate(${d.x},${d.y})`);
}

// ── Drag ───────────────────────────────────────────────────

function dragStart(event, d) {
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x;
    d.fy = d.y;
}

function dragging(event, d) {
    d.fx = event.x;
    d.fy = event.y;
}

function dragEnd(event, d) {
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
}

// ── Interactions ───────────────────────────────────────────

function onNodeClick(d) {
    if (selectedNode) {
        g.selectAll('.node circle').classed('dimmed', false).classed('highlighted', false);
        g.selectAll('.link').classed('highlighted', false);
    }
    selectedNode = d;

    const neighborIds = new Set([d.id]);
    linksData.forEach(l => {
        const sid = typeof l.source === 'object' ? l.source.id : l.source;
        const tid = typeof l.target === 'object' ? l.target.id : l.target;
        if (sid === d.id) neighborIds.add(tid);
        if (tid === d.id) neighborIds.add(sid);
    });

    g.selectAll('.node circle')
        .classed('dimmed', n => !neighborIds.has(n.id))
        .classed('highlighted', n => n.id === d.id);

    g.selectAll('.link')
        .classed('highlighted', l => {
            const sid = typeof l.source === 'object' ? l.source.id : l.source;
            const tid = typeof l.target === 'object' ? l.target.id : l.target;
            return sid === d.id || tid === d.id;
        });

    showNodeDetail(d);
}

async function showNodeDetail(d) {
    const panel = document.getElementById('detail-panel');
    const content = document.getElementById('detail-content');
    panel.classList.remove('hidden');

    content.innerHTML = `
        <div class="space-y-3">
            <div>
                <p class="text-xs text-slate-500">名称</p>
                <p class="text-white font-medium text-sm">${d.label}</p>
            </div>
            <div>
                <p class="text-xs text-slate-500">文件</p>
                <p class="text-slate-300 text-xs font-mono break-all">${d.source_file || '-'}</p>
            </div>
            <div class="grid grid-cols-2 gap-2">
                <div class="bg-slate-800/50 rounded p-2">
                    <p class="text-xs text-slate-500">社区</p>
                    <p class="text-white text-xs">${d.community_name || 'Community ' + d.community}</p>
                </div>
                <div class="bg-slate-800/50 rounded p-2">
                    <p class="text-xs text-slate-500">连接度</p>
                    <p class="text-white text-xs">${d.degree}</p>
                </div>
                <div class="bg-slate-800/50 rounded p-2">
                    <p class="text-xs text-slate-500">类型</p>
                    <p class="text-white text-xs">${d.file_type || '-'}</p>
                </div>
                <div class="bg-slate-800/50 rounded p-2">
                    <p class="text-xs text-slate-500">位置</p>
                    <p class="text-white text-xs">${d.source_location || '-'}</p>
                </div>
            </div>
            <hr class="border-slate-800">
            <div>
                <p class="text-xs text-slate-500 mb-1">邻居节点</p>
                <div id="neighbor-list" class="space-y-0.5 text-xs text-slate-400">加载中…</div>
            </div>
        </div>
    `;

    try {
        const detail = await apiFetch(`/api/repos/${REPO_OWNER}/${REPO_NAME}/node/${encodeURIComponent(d.id)}`);
        const list = document.getElementById('neighbor-list');
        if (detail.neighbors && detail.neighbors.length > 0) {
            list.innerHTML = detail.neighbors.map(n =>
                `<button onclick="focusNode('${n.id}')" class="god-node-btn truncate">
                    <span class="text-white">${n.label}</span>
                    <span class="text-slate-500 ml-1">${n.source_file ? '· ' + n.source_file.split('/').pop() : ''}</span>
                </button>`
            ).join('');
        } else {
            list.innerHTML = '<p class="text-slate-600">无邻居节点</p>';
        }
    } catch (e) {
        document.getElementById('neighbor-list').innerHTML = '<p class="text-red-400">加载失败</p>';
    }
}

function closeDetail() {
    document.getElementById('detail-panel').classList.add('hidden');
    selectedNode = null;
    g.selectAll('.node circle').classed('dimmed', false).classed('highlighted', false);
    g.selectAll('.link').classed('highlighted', false);
}

// ── Tooltip ────────────────────────────────────────────────

let tooltipEl = null;

function showTooltip(event, d) {
    if (!tooltipEl) {
        tooltipEl = document.createElement('div');
        tooltipEl.style.cssText = 'position:fixed;z-index:50;background:#0f172a;border:1px solid #334155;border-radius:0.5rem;padding:0.5rem 0.75rem;font-size:0.75rem;box-shadow:0 10px 15px -3px rgba(0,0,0,0.3);pointer-events:none;max-width:250px';
        document.body.appendChild(tooltipEl);
    }
    tooltipEl.innerHTML = `
        <p style="color:#fff;font-weight:500">${d.label}</p>
        <p style="color:#94a3b8">${d.source_file || ''}</p>
        <p style="color:#64748b">${d.community_name || 'Community ' + d.community} · degree ${d.degree}</p>
    `;
    tooltipEl.style.left = (event.clientX + 12) + 'px';
    tooltipEl.style.top = (event.clientY - 10) + 'px';
    tooltipEl.style.display = 'block';
}

function hideTooltip() {
    if (tooltipEl) tooltipEl.style.display = 'none';
}

// ── Search ─────────────────────────────────────────────────

function onSearch(query) {
    query = query.toLowerCase().trim();
    g.selectAll('.node circle').classed('search-match', false);
    if (!query) return;

    const matches = nodesData.filter(n =>
        n.label.toLowerCase().includes(query) ||
        (n.source_file && n.source_file.toLowerCase().includes(query))
    );

    g.selectAll('.node circle')
        .classed('search-match', d => matches.some(m => m.id === d.id));

    if (matches.length > 0) {
        const first = matches[0];
        const transform = d3.zoomTransform(svgEl.node());
        const x = width / 2 - first.x * transform.k;
        const y = height / 2 - first.y * transform.k;
        svgEl.transition().duration(500)
            .call(zoomBehavior.transform, d3.zoomIdentity.translate(x, y).scale(transform.k));
    }
}

// ── Focus node ─────────────────────────────────────────────

function focusNode(nodeId) {
    const node = nodesData.find(n => n.id === nodeId);
    if (!node) return;

    const transform = d3.zoomTransform(svgEl.node());
    const x = width / 2 - node.x * transform.k;
    const y = height / 2 - node.y * transform.k;
    svgEl.transition().duration(500)
        .call(zoomBehavior.transform, d3.zoomIdentity.translate(x, y).scale(transform.k));

    onNodeClick(node);
}

// ── Filters ────────────────────────────────────────────────

let filterDebounce = null;
function applyFilters() {
    clearTimeout(filterDebounce);
    filterDebounce = setTimeout(() => {
        document.getElementById('graph-loading')?.classList.remove('hidden');
        loadGraphData();
    }, 300);
}

// ── Zoom controls ──────────────────────────────────────────

function zoomIn() {
    svgEl.transition().duration(300).call(zoomBehavior.scaleBy, 1.5);
}
function zoomOut() {
    svgEl.transition().duration(300).call(zoomBehavior.scaleBy, 0.67);
}
function zoomReset() {
    svgEl.transition().duration(500).call(zoomBehavior.transform, d3.zoomIdentity);
}

/* graph_viewer.js — D3.js force-directed graph viewer */

let svg, g, simulation, zoom;
let nodesData = [], linksData = [];
let nodeElements, linkElements, labelElements;
let selectedNode = null;

const colorScale = d3.scaleOrdinal(d3.schemeTableau10);
const container = document.getElementById('graph-container');
const width = container.clientWidth;
const height = container.clientHeight;

// ── Init ───────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    initSVG();
    loadGraphData();
});

function initSVG() {
    svg = d3.select('#graph-svg')
        .attr('width', width)
        .attr('height', height);

    // Zoom behavior
    zoom = d3.zoom()
        .scaleExtent([0.1, 10])
        .on('zoom', (event) => {
            g.attr('transform', event.transform);
            // Show labels only when zoomed in enough
            const k = event.transform.k;
            g.selectAll('.node text')
                .attr('display', k > 1.5 || d3.select(event.target).datum()?.degree > 10 ? null : 'none');
        });

    svg.call(zoom);

    g = svg.append('g');

    // Arrow marker for directed edges (optional)
    svg.append('defs').append('marker')
        .attr('id', 'arrow')
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 15)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', '#475569');
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
        const data = await apiFetch(`/api/graphs/${GRAPH_ID}/d3-data?${params}`);
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
    // Clear previous
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

    // Position node groups via transform (circles/text are relative to group origin)
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
    // Deselect previous
    if (selectedNode) {
        g.selectAll('.node circle').classed('dimmed', false).classed('highlighted', false);
        g.selectAll('.link').classed('highlighted', false);
    }

    selectedNode = d;

    // Find neighbors
    const neighborIds = new Set();
    neighborIds.add(d.id);
    linksData.forEach(l => {
        const sid = typeof l.source === 'object' ? l.source.id : l.source;
        const tid = typeof l.target === 'object' ? l.target.id : l.target;
        if (sid === d.id) neighborIds.add(tid);
        if (tid === d.id) neighborIds.add(sid);
    });

    // Highlight
    g.selectAll('.node circle')
        .classed('dimmed', n => !neighborIds.has(n.id))
        .classed('highlighted', n => n.id === d.id);

    g.selectAll('.link')
        .classed('highlighted', l => {
            const sid = typeof l.source === 'object' ? l.source.id : l.source;
            const tid = typeof l.target === 'object' ? l.target.id : l.target;
            return sid === d.id || tid === d.id;
        });

    // Show detail panel
    showNodeDetail(d);
}

async function showNodeDetail(d) {
    const panel = document.getElementById('detail-panel');
    const content = document.getElementById('detail-content');
    panel.classList.remove('hidden');

    content.innerHTML = `
        <div class="space-y-3">
            <div>
                <p class="text-xs text-dark-200">名称</p>
                <p class="text-white font-medium">${d.label}</p>
            </div>
            <div>
                <p class="text-xs text-dark-200">文件</p>
                <p class="text-white text-xs font-mono">${d.source_file || '-'}</p>
            </div>
            <div class="grid grid-cols-2 gap-2">
                <div>
                    <p class="text-xs text-dark-200">社区</p>
                    <p class="text-white text-xs">${d.community_name || 'Community ' + d.community}</p>
                </div>
                <div>
                    <p class="text-xs text-dark-200">连接度</p>
                    <p class="text-white text-xs">${d.degree}</p>
                </div>
                <div>
                    <p class="text-xs text-dark-200">类型</p>
                    <p class="text-white text-xs">${d.file_type || '-'}</p>
                </div>
                <div>
                    <p class="text-xs text-dark-200">位置</p>
                    <p class="text-white text-xs">${d.source_location || '-'}</p>
                </div>
            </div>
            <hr class="border-dark-700">
            <div>
                <p class="text-xs text-dark-200 mb-1">邻居节点</p>
                <div id="neighbor-list" class="space-y-1 text-xs text-dark-200">加载中…</div>
            </div>
        </div>
    `;

    // Fetch full detail
    try {
        const detail = await apiFetch(`/api/graphs/${GRAPH_ID}/node/${encodeURIComponent(d.id)}`);
        const list = document.getElementById('neighbor-list');
        if (detail.neighbors && detail.neighbors.length > 0) {
            list.innerHTML = detail.neighbors.map(n =>
                `<button onclick="focusNode('${n.id}')" class="block w-full text-left px-2 py-1 rounded hover:bg-dark-700 text-dark-200 hover:text-white transition-colors truncate">
                    ${n.label} <span class="opacity-40">${n.source_file ? '· ' + n.source_file.split('/').pop() : ''}</span>
                </button>`
            ).join('');
        } else {
            list.innerHTML = '<p class="opacity-40">无邻居节点</p>';
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
        tooltipEl.className = 'fixed z-50 bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-xs shadow-xl pointer-events-none';
        document.body.appendChild(tooltipEl);
    }
    tooltipEl.innerHTML = `
        <p class="text-white font-medium">${d.label}</p>
        <p class="text-dark-200">${d.source_file || ''}</p>
        <p class="text-dark-200">${d.community_name || 'Community ' + d.community} · degree ${d.degree}</p>
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

    // Pan to first match
    if (matches.length > 0) {
        const first = matches[0];
        const transform = d3.zoomTransform(svg.node());
        const x = width / 2 - first.x * transform.k;
        const y = height / 2 - first.y * transform.k;
        svg.transition().duration(500)
            .call(zoom.transform, d3.zoomIdentity.translate(x, y).scale(transform.k));
    }
}

// ── Focus node (from god nodes list) ───────────────────────

function focusNode(nodeId) {
    const node = nodesData.find(n => n.id === nodeId);
    if (!node) return;

    // Pan to node
    const transform = d3.zoomTransform(svg.node());
    const x = width / 2 - node.x * transform.k;
    const y = height / 2 - node.y * transform.k;
    svg.transition().duration(500)
        .call(zoom.transform, d3.zoomIdentity.translate(x, y).scale(transform.k));

    // Simulate click
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
    svg.transition().duration(300).call(zoom.scaleBy, 1.5);
}

function zoomOut() {
    svg.transition().duration(300).call(zoom.scaleBy, 0.67);
}

function zoomReset() {
    svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
}

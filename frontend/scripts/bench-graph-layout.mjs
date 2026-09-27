/**
 * Headless Cytoscape layout cost for synthetic graphs of 100 / 300 / 600 / 1200
 * elements (the canvas cap), using the same first-render layout options as
 * components/graph/CytoscapeCanvas.tsx. Measures layout computation only --
 * no painting, and Node rather than a browser -- so it bounds the CPU cost of
 * the layout step, not end-to-end render time.
 *
 *   node scripts/bench-graph-layout.mjs
 */
import cytoscape from "cytoscape";

const TRIALS = 5;

function synthetic(elementCount) {
  // A hub-and-spoke tree plus cross links, about half nodes and half edges.
  const nodes = Math.ceil(elementCount / 2);
  const els = [];
  for (let i = 0; i < nodes; i++) els.push({ group: "nodes", data: { id: `n${i}`, label: `Entity ${i}` } });
  let edges = 0;
  for (let i = 1; i < nodes && els.length < elementCount; i++, edges++) {
    els.push({ group: "edges", data: { id: `e${edges}`, source: `n${Math.floor((i - 1) / 4)}`, target: `n${i}` } });
  }
  for (let i = 0; els.length < elementCount; i++, edges++) {
    els.push({ group: "edges", data: { id: `e${edges}`, source: `n${i % nodes}`, target: `n${(i * 7 + 3) % nodes}` } });
  }
  return els;
}

const results = {};
for (const size of [100, 300, 600, 1200]) {
  const times = [];
  for (let t = 0; t < TRIALS; t++) {
    const cy = cytoscape({ headless: true, styleEnabled: true, elements: synthetic(size) });
    const start = performance.now();
    cy.layout({ name: "cose", animate: false, nodeDimensionsIncludeLabels: true, randomize: true }).run();
    times.push(performance.now() - start);
    cy.destroy();
  }
  times.sort((a, b) => a - b);
  results[size] = { min: Math.round(times[0]), median: Math.round(times[Math.floor(TRIALS / 2)]), max: Math.round(times[TRIALS - 1]) };
}
console.log(JSON.stringify({ node: process.version, trials: TRIALS, layout_ms_by_elements: results }, null, 2));

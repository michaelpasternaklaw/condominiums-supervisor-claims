let documents = null;
let loadingPromise = null;

const normalize = (value) => (value || "")
  .normalize("NFKD")
  .replace(/[\u0591-\u05C7]/g, "")
  .replace(/[\u200e\u200f\u202a-\u202e]/g, "")
  .replace(/[^\p{L}\p{N}]+/gu, " ")
  .trim()
  .toLocaleLowerCase("he");

async function loadIndex() {
  if (documents) return documents;
  if (loadingPromise) return loadingPromise;
  loadingPromise = (async () => {
    const manifestResponse = await fetch("data/search-manifest.json");
    if (!manifestResponse.ok) throw new Error("manifest");
    const manifest = await manifestResponse.json();
    const loaded = [];
    for (let index = 0; index < manifest.chunks.length; index += 1) {
      const response = await fetch(manifest.chunks[index].file);
      if (!response.ok) throw new Error("chunk");
      const chunk = await response.json();
      for (const item of chunk) loaded.push({ id: item.id, raw: item.text, normalized: normalize(item.text) });
      postMessage({ type: "progress", loaded: index + 1, total: manifest.chunks.length });
    }
    documents = loaded;
    return documents;
  })();
  return loadingPromise;
}

function makeSnippet(text, token) {
  const plain = text.replace(/=== PDF PAGE \d+ ===/g, " ").replace(/\s+/g, " ").trim();
  const normalizedPlain = normalize(plain);
  const position = normalizedPlain.indexOf(token);
  if (position < 0) return plain.slice(0, 240);
  const start = Math.max(0, position - 85);
  const end = Math.min(plain.length, position + 170);
  return `${start ? "…" : ""}${plain.slice(start, end)}${end < plain.length ? "…" : ""}`;
}

self.onmessage = async (event) => {
  if (event.data.type !== "search") return;
  const requestId = event.data.requestId;
  try {
    const query = normalize(event.data.query);
    const tokens = query.split(/\s+/).filter(Boolean);
    if (!tokens.length) {
      postMessage({ type: "results", requestId, matches: [] });
      return;
    }
    const index = await loadIndex();
    const matches = [];
    for (const item of index) {
      if (tokens.every((token) => item.normalized.includes(token))) {
        matches.push({ id: item.id, snippet: makeSnippet(item.raw, tokens[0]) });
      }
    }
    postMessage({ type: "results", requestId, matches });
  } catch (error) {
    postMessage({ type: "error", requestId });
  }
};

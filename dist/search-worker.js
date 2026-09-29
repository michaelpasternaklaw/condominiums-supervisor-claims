let manifestPromise = null;
const tokenCache = new Map();
const pageCache = new Map();

const SYNONYMS = {
  "מזגן": ["מזגן", "מיזוג", "מעבה", "רטט", "רעש"],
  "רעש": ["רעש", "רטט", "מטרד", "אקוסטי"],
  "מים": ["מים", "רטיבות", "נזילה", "ניקוז", "מרזב", "צנרת"],
  "רטיבות": ["רטיבות", "נזילה", "איטום", "מים"],
  "מצלמה": ["מצלמה", "צילום", "פרטיות", "התחקות"],
  "פרגולה": ["פרגולה", "גגון", "סככה", "גג"],
  "חצר": ["חצר", "גינה", "הצמדה"],
  "סמכות": ["סמכות", "סעיף 72", "פיצול סעדים"],
  "הצמדה": ["הצמדה", "מוצמד", "רכוש משותף"],
};

const normalize = (value) => String(value || "")
  .normalize("NFKD")
  .replace(/[\u0591-\u05C7]/g, "")
  .replace(/[\u200e\u200f\u202a-\u202e]/g, "")
  .replace(/[^\p{L}\p{N}]+/gu, " ")
  .trim()
  .toLocaleLowerCase("he");

function bucketFor(value, count) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash % count;
}

async function getManifest() {
  if (!manifestPromise) manifestPromise = fetch("data/search-manifest.json").then((response) => {
    if (!response.ok) throw new Error("manifest");
    return response.json();
  });
  return manifestPromise;
}

async function loadTokenBucket(index, manifest) {
  if (!tokenCache.has(index)) {
    tokenCache.set(index, fetch(manifest.tokenFiles[index]).then((response) => {
      if (!response.ok) throw new Error("token bucket");
      return response.json();
    }));
  }
  return tokenCache.get(index);
}

async function loadPageShard(index, manifest) {
  if (!pageCache.has(index)) {
    pageCache.set(index, fetch(manifest.pageFiles[index]).then((response) => {
      if (!response.ok) throw new Error("page shard");
      return response.json();
    }));
  }
  return pageCache.get(index);
}

function parseQuery(raw) {
  return raw.split(/\s+(?:OR|או)\s+/i).map((part) => {
    const phrases = [...part.matchAll(/"([^"]+)"/g)].map((match) => normalize(match[1])).filter(Boolean);
    const remainder = normalize(part.replace(/"[^"]+"/g, " "));
    const tokens = remainder.split(/\s+/).filter(Boolean);
    return { phrases, tokens };
  }).filter((group) => group.phrases.length || group.tokens.length);
}

async function postingsForTerm(term, manifest) {
  const normalized = normalize(term);
  const index = bucketFor(normalized, manifest.tokenBucketCount);
  const bucket = await loadTokenBucket(index, manifest);
  let postings = bucket[normalized];
  if (!postings && normalized.length >= 4) {
    const matchingKeys = Object.keys(bucket).filter((key) => key.startsWith(normalized)).slice(0, 20);
    postings = matchingKeys.flatMap((key) => bucket[key]);
  }
  const map = new Map();
  for (const [id, pages] of postings || []) {
    if (!map.has(id)) map.set(id, new Set());
    pages.forEach((page) => map.get(id).add(page));
  }
  return map;
}

async function postingsForAtom(atom, manifest) {
  const variants = SYNONYMS[atom] || [atom];
  const maps = await Promise.all(variants.map((variant) => postingsForTerm(variant, manifest)));
  const combined = new Map();
  for (const map of maps) {
    for (const [id, pages] of map) {
      if (!combined.has(id)) combined.set(id, new Set());
      pages.forEach((page) => combined.get(id).add(page));
    }
  }
  return combined;
}

function intersect(left, right) {
  if (!left) return right;
  const result = new Map();
  for (const [id, leftPages] of left) {
    if (!right.has(id)) continue;
    const rightPages = right.get(id);
    const common = new Set([...leftPages].filter((page) => rightPages.has(page)));
    result.set(id, common.size ? common : new Set([...leftPages, ...rightPages]));
  }
  return result;
}

function union(into, from) {
  for (const [id, pages] of from) {
    if (!into.has(id)) into.set(id, new Set());
    pages.forEach((page) => into.get(id).add(page));
  }
}

function snippet(text, phrase) {
  const plain = String(text || "").replace(/\s+/g, " ").trim();
  const normalized = normalize(plain);
  const position = Math.max(0, normalized.indexOf(normalize(phrase)));
  const start = Math.max(0, position - 95);
  const end = Math.min(plain.length, position + 220);
  return `${start ? "…" : ""}${plain.slice(start, end)}${end < plain.length ? "…" : ""}`;
}

async function verifyPhrases(candidates, phrases, manifest) {
  if (!phrases.length) return candidates;
  const result = new Map();
  const shardIds = [...new Set([...candidates.keys()].map((id) => manifest.documentShards[id]).filter((value) => value !== undefined))];
  const shards = new Map(await Promise.all(shardIds.map(async (index) => [index, await loadPageShard(index, manifest)])));
  for (const [id] of candidates) {
    const shard = shards.get(manifest.documentShards[id]);
    const pages = shard?.[id] || [];
    for (const [pageNumber, pageText] of pages) {
      const normalizedPage = normalize(pageText);
      if (phrases.every((phrase) => normalizedPage.includes(phrase))) {
        result.set(id, { pages: new Set([pageNumber]), snippet: snippet(pageText, phrases[0]) });
        break;
      }
    }
  }
  return result;
}

async function searchGroup(group, manifest) {
  const atoms = [...group.tokens, ...group.phrases.flatMap((phrase) => phrase.split(/\s+/))];
  let candidates = null;
  for (const atom of atoms) candidates = intersect(candidates, await postingsForAtom(atom, manifest));
  candidates ||= new Map();
  if (!group.phrases.length) return new Map([...candidates].map(([id, pages]) => [id, { pages, snippet: "" }]));
  return verifyPhrases(candidates, group.phrases, manifest);
}

self.onmessage = async (event) => {
  if (event.data.type !== "search") return;
  const { requestId, query } = event.data;
  try {
    const groups = parseQuery(query);
    if (!groups.length) {
      postMessage({ type: "results", requestId, matches: [] });
      return;
    }
    const manifest = await getManifest();
    postMessage({ type: "progress", requestId, message: "מחפש במקטעי הטקסט הרלוונטיים…" });
    const combined = new Map();
    for (const group of groups) {
      const result = await searchGroup(group, manifest);
      for (const [id, value] of result) {
        if (!combined.has(id)) combined.set(id, { pages: new Set(), snippet: "" });
        value.pages.forEach((page) => combined.get(id).pages.add(page));
        if (!combined.get(id).snippet && value.snippet) combined.get(id).snippet = value.snippet;
      }
    }
    const matches = [...combined].map(([id, value]) => ({ id, pages: [...value.pages].sort((a, b) => a - b).slice(0, 8), snippet: value.snippet }));
    postMessage({ type: "results", requestId, matches });
  } catch (error) {
    postMessage({ type: "error", requestId, message: String(error) });
  }
};

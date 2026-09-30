import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const dist = path.join(root, "dist");
const project = path.resolve(root, "../../..");
const catalog = JSON.parse(fs.readFileSync(path.join(dist, "data/catalog.json"), "utf8"));
const manifest = JSON.parse(fs.readFileSync(path.join(dist, "data/search-manifest.json"), "utf8"));
const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
const app = fs.readFileSync(path.join(dist, "app.js"), "utf8");

assert.equal(catalog.totalDocuments, 1799);
assert.equal(catalog.officialDocuments, 1765);
assert.equal(catalog.externalDocuments, 34);
assert.equal(catalog.fullTextDocuments, 1763);
assert.equal(catalog.localPdfDocuments, 1763);
assert.equal(catalog.docxDocuments, 1763);
assert.equal(catalog.ocrDocuments, 20);
assert.equal(catalog.ashdodOfficial, 58);
assert.equal(catalog.focusRuleVersion, "2026.09.30.1");
assert.equal(catalog.focusCounts.camera, 63);
assert.ok(catalog.focusCounts.camera < 100, "camera focus must remain narrow");
assert.equal(manifest.documents, 1763);
assert.equal(manifest.tokenBucketCount, 64);
assert.equal(manifest.pageShardCount, 64);
assert.equal(manifest.evidenceShardCount, 64);
assert.ok(manifest.version);
assert.equal(manifest.ruleVersion, "2026.09.29.1");
assert.ok(!fs.existsSync(path.join(dist, "texts")), "full text must not be duplicated as standalone files");

const requiredIds = ["query", "municipality-filter", "adjudicator-filter", "status-filter", "confidence-filter", "availability-filter", "word-filter", "view-compare", "view-workspace", "view-methodology", "secure-dialog", "attach-dialog"];
for (const id of requiredIds) assert.match(html, new RegExp(`id=["']${id}["']`));
const cacheBlock = app.match(/function cacheElements\(\) \{([\s\S]*?)\.forEach/)?.[1] || "";
const cachedIds = [...cacheBlock.matchAll(/"([a-z][a-z0-9-]+)"/g)].map((match) => match[1]);
for (const id of cachedIds) assert.match(html, new RegExp(`id=["']${id}["']`), `missing cached element ${id}`);
assert.match(app, /record\.duplicateGroup \|\| record\.isTest/);
assert.doesNotMatch(app, /fetch\(["']https?:\/\//);
assert.doesNotMatch(app, /קטע אופרטיבי לאיתור/);
assert.match(app, /comparison-board/);
assert.match(app, /record\.focusTopics/);
assert.doesNotMatch(app, /LENSES\[state\.lens\]/);
assert.doesNotMatch(app, /record\.operativeExcerpt, record\.sourceName/);
assert.match(app, /השורה התחתונה/);
assert.doesNotMatch(app, /comparison-grid/);
assert.match(html, /class="comparison-surface"/);

const ashdod = catalog.records.filter((record) => record.ashdodRelation);
assert.equal(ashdod.filter((record) => record.sourceStatus === "רשמי").length, 58);
assert.equal(ashdod.filter((record) => record.sourceStatus === "משוחזר ממקור מאוחר").length, 4);
assert.equal(ashdod.filter((record) => record.sourceStatus === "ליד מחקרי").length, 1);
assert.ok(ashdod.filter((record) => record.sourceStatus === "משוחזר ממקור מאוחר").every((record) => record.verificationRank === 2));

const known = catalog.records.find((record) => record.caseNumber === "12/45/2023");
assert.ok(known?.hasFullText);
assert.ok(known.ashdodRelation);
assert.ok(known.classificationConfidence);
assert.ok(known.sectionRole);
assert.ok(known.ruleVersion);
assert.ok(Array.isArray(known.evidencePages));
assert.ok(known.documentAvailability);
assert.ok(known.documentUrl);
assert.equal(known.localAssetUrl, known.pdf);
assert.ok(known.hasDocx);
assert.match(known.docx, /^docx\/.+\.docx$/);

const evidenceShard = manifest.documentEvidenceShards[known.id];
const evidence = JSON.parse(fs.readFileSync(path.join(dist, manifest.evidenceFiles[evidenceShard]), "utf8"))[known.id];
assert.ok(evidence.classifications.length > 0);
assert.ok(evidence.evidenceSnippets.length > 0);
assert.ok(evidence.classifications.every((item) => item.topic && item.confidence && item.sectionRole && Array.isArray(item.pages)));

const cameraFocused = catalog.records.find((record) => record.id === "official-1667");
assert.ok(cameraFocused?.focusTopics.includes("camera"));
const cameraSummary = cameraFocused.focusClassifications.find((item) => item.key === "camera");
assert.ok(cameraSummary.pages.length > 0);
assert.match(cameraSummary.reason, /מופעי עוגן/);
const cameraEvidenceShard = manifest.documentEvidenceShards[cameraFocused.id];
const cameraEvidence = JSON.parse(fs.readFileSync(path.join(dist, manifest.evidenceFiles[cameraEvidenceShard]), "utf8"))[cameraFocused.id];
assert.ok(cameraEvidence.focusEvidence.find((item) => item.key === "camera")?.evidence.length > 0);

const operative = catalog.records.find((record) => record.caseNumber === "7/254/2024");
assert.equal(operative?.outcome, "התביעה התקבלה בעיקרה");
assert.deepEqual(operative?.operativePages, [18, 19]);
assert.match(operative?.relief || "", /הריסה/);
assert.match(operative?.operativeExcerpt || "", /לאור כל האמור לעיל/);
assert.doesNotMatch(operative?.operativeExcerpt || "", /^מדינת ישראל/);
assert.ok(catalog.records.some((record) => (record.defenseTopics || []).length > 0));
assert.ok(catalog.records.some((record) => record.textMethod === "OCR"));

function normalize(value) {
  return String(value || "").normalize("NFKD").replace(/[\u0591-\u05C7]/g, "").replace(/[\u200e\u200f\u202a-\u202e]/g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim().toLocaleLowerCase("he");
}
function bucketFor(value, count) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) { hash ^= value.charCodeAt(index); hash = Math.imul(hash, 16777619) >>> 0; }
  return hash % count;
}
const term = normalize("פרגולה");
const bucket = bucketFor(term, manifest.tokenBucketCount);
const tokenData = JSON.parse(fs.readFileSync(path.join(dist, manifest.tokenFiles[bucket]), "utf8"));
assert.ok(tokenData[term]?.length > 0);
assert.ok(tokenData[term].some(([id, pages]) => id === known.id && pages.length));

const workerMessages = [];
globalThis.self = globalThis;
globalThis.postMessage = (message) => workerMessages.push(message);
globalThis.fetch = async (relativePath) => ({
  ok: true,
  json: async () => JSON.parse(fs.readFileSync(path.join(dist, relativePath), "utf8")),
});
await import(`${pathToFileURL(path.join(dist, "search-worker.js")).href}?smoke=1`);
await globalThis.onmessage({ data: { type: "search", requestId: 77, query: '"רכוש משותף"' } });
const workerResult = workerMessages.find((message) => message.type === "results" && message.requestId === 77);
assert.ok(workerResult?.matches?.length > 0);
assert.ok(workerResult.matches.some((match) => match.pages.length > 0));

for (const relativePath of [...manifest.tokenFiles, ...manifest.pageFiles, ...manifest.evidenceFiles]) {
  assert.ok(!relativePath.startsWith("/"), `asset path must support a GitHub Pages subpath: ${relativePath}`);
  assert.ok(fs.existsSync(path.join(dist, relativePath)), `missing static asset ${relativePath}`);
}
for (const record of catalog.records.filter((item) => item.localAssetUrl)) {
  assert.ok(!record.localAssetUrl.startsWith("/"));
  assert.ok(fs.existsSync(path.join(dist, record.localAssetUrl)), `missing document ${record.localAssetUrl}`);
}
for (const record of catalog.records.filter((item) => item.hasDocx)) {
  assert.ok(!record.docx.startsWith("/"));
  assert.ok(fs.existsSync(path.join(dist, record.docx)), `missing DOCX ${record.docx}`);
  const signature = fs.readFileSync(path.join(dist, record.docx)).subarray(0, 2).toString("ascii");
  assert.equal(signature, "PK", `invalid DOCX container ${record.docx}`);
}
assert.equal(fs.readdirSync(path.join(dist, "docx")).filter((name) => name.endsWith(".docx")).length, 1763);

const files = fs.readdirSync(dist, { recursive: true, withFileTypes: true })
  .filter((entry) => entry.isFile()).map((entry) => path.join(entry.parentPath || entry.path, entry.name));
const stats = files.map((file) => fs.statSync(file));
assert.ok(stats.reduce((sum, item) => sum + item.size, 0) <= 850 * 1024 * 1024);
assert.ok(Math.max(...stats.map((item) => item.size)) <= 90 * 1024 * 1024);
assert.ok(stats.every((item) => item.nlink === 1), "Pages artifact must not contain hard links");

const seedPath = path.join(project, "case_management/private_workspace_seed.json");
const seed = fs.existsSync(seedPath) ? JSON.parse(fs.readFileSync(seedPath, "utf8")) : { version: 1, title: "בדיקה", claims: [] };
if (fs.existsSync(seedPath)) {
  assert.equal(seed.claims.length, 7);
  assert.ok(seed.claims.every((claim) => claim.id && claim.fact && claim.missingEvidence && claim.forum));
}
assert.ok(!fs.existsSync(path.join(dist, "private_workspace_seed.json")));

const subtle = crypto.webcrypto.subtle;
const encoder = new TextEncoder();
const salt = crypto.randomBytes(16);
const iv = crypto.randomBytes(12);
const material = await subtle.importKey("raw", encoder.encode("בדיקת-סיסמה-123"), "PBKDF2", false, ["deriveKey"]);
const key = await subtle.deriveKey({ name: "PBKDF2", salt, iterations: 180000, hash: "SHA-256" }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
const encrypted = await subtle.encrypt({ name: "AES-GCM", iv }, key, encoder.encode(JSON.stringify(seed)));
const decrypted = await subtle.decrypt({ name: "AES-GCM", iv }, key, encrypted);
assert.deepEqual(JSON.parse(new TextDecoder().decode(decrypted)), seed);

console.log(JSON.stringify({
  records: catalog.totalDocuments,
  fullText: catalog.fullTextDocuments,
  docx: catalog.docxDocuments,
  ashdod: { official: 58, reconstructed: 4, leads: 1 },
  searchTerm: term,
  searchPostings: tokenData[term].length,
  phraseSearchMatches: workerResult.matches.length,
  evidenceTopics: evidence.classifications.length,
  staticSiteMiB: Math.round(stats.reduce((sum, item) => sum + item.size, 0) / 1024 / 1024),
  privateSeedClaims: fs.existsSync(seedPath) ? seed.claims.length : "local-only",
  encryptionRoundTrip: true,
}, null, 2));

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
assert.equal(catalog.fullTextDocuments, 513);
assert.equal(catalog.ashdodOfficial, 58);
assert.equal(manifest.documents, 513);
assert.equal(manifest.tokenBucketCount, 64);
assert.equal(manifest.pageShardCount, 24);

const requiredIds = ["query", "municipality-filter", "adjudicator-filter", "status-filter", "view-compare", "view-workspace", "view-methodology", "secure-dialog", "attach-dialog"];
for (const id of requiredIds) assert.match(html, new RegExp(`id=["']${id}["']`));
const cacheBlock = app.match(/function cacheElements\(\) \{([\s\S]*?)\.forEach/)?.[1] || "";
const cachedIds = [...cacheBlock.matchAll(/"([a-z][a-z0-9-]+)"/g)].map((match) => match[1]);
for (const id of cachedIds) assert.match(html, new RegExp(`id=["']${id}["']`), `missing cached element ${id}`);
assert.match(app, /record\.duplicateGroup \|\| record\.isTest/);
assert.doesNotMatch(app, /fetch\(["']https?:\/\//);
assert.doesNotMatch(app, /קטע אופרטיבי לאיתור/);

const ashdod = catalog.records.filter((record) => record.ashdodRelation);
assert.equal(ashdod.filter((record) => record.sourceStatus === "רשמי").length, 58);
assert.equal(ashdod.filter((record) => record.sourceStatus === "משוחזר ממקור מאוחר").length, 4);
assert.equal(ashdod.filter((record) => record.sourceStatus === "ליד מחקרי").length, 1);
assert.ok(ashdod.filter((record) => record.sourceStatus === "משוחזר ממקור מאוחר").every((record) => record.verificationRank === 2));

const known = catalog.records.find((record) => record.caseNumber === "12/45/2023");
assert.ok(known?.hasFullText);
assert.ok(known.ashdodRelation);

const operative = catalog.records.find((record) => record.caseNumber === "7/254/2024");
assert.equal(operative?.outcome, "התביעה התקבלה בעיקרה");
assert.deepEqual(operative?.operativePages, [18, 19]);
assert.match(operative?.relief || "", /הריסה/);
assert.match(operative?.operativeExcerpt || "", /לאור כל האמור לעיל/);
assert.doesNotMatch(operative?.operativeExcerpt || "", /^מדינת ישראל/);

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
  ashdod: { official: 58, reconstructed: 4, leads: 1 },
  searchTerm: term,
  searchPostings: tokenData[term].length,
  phraseSearchMatches: workerResult.matches.length,
  privateSeedClaims: fs.existsSync(seedPath) ? seed.claims.length : "local-only",
  encryptionRoundTrip: true,
}, null, 2));

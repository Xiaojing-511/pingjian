import { readFileSync } from "node:fs"
import { parseArrayBuffer } from "../src/import/clean.ts"

const file = readFileSync(new URL("../files/exportdou-6b7f0fec-f310-49a3-a302-8063863efc01.csv", import.meta.url))
const parsed = parseArrayBuffer(file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength))
const pureImage = parsed.comments.filter((comment) => comment.aiText === "[图片]" || comment.rawText.startsWith("[图片]\n"))
const stats = parsed.cleanStats

console.log(JSON.stringify({
  videoId: parsed.videoId,
  author: parsed.author,
  title: parsed.title,
  stats,
  images: parsed.comments.filter((comment) => comment.hasImage).length,
  low: parsed.comments.filter((comment) => comment.lowInformation).length,
}, null, 2))

if (parsed.videoId !== "7696144437814208872") throw new Error(`video id ${parsed.videoId}`)
if (parsed.author !== "火力羊子") throw new Error(`author ${parsed.author}`)
if (pureImage.length) throw new Error("pure image comments were kept")
if (stats.kept !== parsed.comments.length) throw new Error("kept mismatch")
if (stats.rawRows !== stats.kept + stats.droppedEmpty + stats.droppedNotLevel1) {
  throw new Error(`row accounting ${JSON.stringify(stats)}`)
}
if (stats.kept >= stats.rawRows) throw new Error("expected some empty comments to be removed")
console.log("clean ok")

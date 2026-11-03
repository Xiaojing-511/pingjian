import { useEffect, useState } from "react"
import { ApiError, saveOpportunity } from "../api"
import type { Claim, Opportunity, Report } from "../types"
import { claimText } from "../types"

export function Opportunities({
  videoId,
  report,
  onSaved,
  onOpenComments,
}: {
  videoId: string
  report: Report
  onSaved: (report: Report) => void
  onOpenComments: (themeId: string) => void
}) {
  if (!report.opportunities.length) {
    return <section className="card">{report.analysisLimit || "还没有选题。"}</section>
  }
  return (
    <div className="stack">
      <p className="muted">
        综合分越高越靠前。可行性和制作成本是模型判断，评论条数和摘录来自原始评论，不能改。
      </p>
      {report.opportunities.map((card) => (
        <CardEditor key={card.themeId} videoId={videoId} card={card} onSaved={onSaved} onOpenComments={onOpenComments} />
      ))}
    </div>
  )
}

function CardEditor({
  videoId,
  card,
  onSaved,
  onOpenComments,
}: {
  videoId: string
  card: Opportunity
  onSaved: (report: Report) => void
  onOpenComments: (themeId: string) => void
}) {
  const [topic, setTopic] = useState(card.topic)
  const [structure, setStructure] = useState(card.structure.join("\n"))
  const [targetNeed, setTargetNeed] = useState(card.targetNeed)
  const [validation, setValidation] = useState(card.validation)
  const [claims, setClaims] = useState(card.claims.map((claim) => claim.text))
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const steps = structure.split("\n")

  useEffect(() => {
    setTopic(card.topic)
    setStructure(card.structure.join("\n"))
    setTargetNeed(card.targetNeed)
    setValidation(card.validation)
    setClaims(card.claims.map((claim) => claim.text))
  }, [card])

  async function save(favorite = card.favorite) {
    setPending(true)
    setError("")
    try {
      const next = await saveOpportunity(videoId, {
        themeId: card.themeId,
        topic,
        structure: structure.split("\n").map((line) => line.trim()).filter(Boolean),
        targetNeed,
        validation,
        favorite,
        claims: claims.map((text, index) => ({ index, text })),
      })
      onSaved(next)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "保存失败")
    } finally {
      setPending(false)
    }
  }

  function updateStep(index: number, value: string) {
    const next = structure.split("\n")
    next[index] = value
    setStructure(next.join("\n"))
  }

  function removeStep(index: number) {
    setStructure(steps.filter((_, item) => item !== index).join("\n"))
  }

  return (
    <article className="card opportunity">
      <div className="opp-head">
        <div className="opp-title">
          <span className="opp-rank">{card.rank}</span>
          <h2>{topic || "未命名选题"}</h2>
        </div>
        {card.favorite ? <span className="badge">已收藏</span> : null}
      </div>
      <div className="opp-stats">
        <Stat label="综合分" value={String(card.score)} note="越高越优先" />
        <Stat label="可行性" value={scaleText(card.feasibility, FEASIBILITY)} note={`${card.feasibility}/5 · 模型判断`} />
        <Stat label="制作成本" value={scaleText(card.cost, COST)} note={`${card.cost}/5 · 模型判断`} />
        <Stat label="评论依据" value={`${Math.round(card.evidence * 100)}%`} note="摘录能对上的比例" />
      </div>
      <p className="quiet">
        点赞较高的样本 {card.sampleCount} 条，这个标签一共 {card.labelTotal} 条。
      </p>

      <section className="opp-section">
        <h3>建议怎么做</h3>
        <label className="field">
          <span className="field-label">选题</span>
          <span className="field-hint">下一条视频可以用的题目</span>
          <input value={topic} onChange={(event) => setTopic(event.target.value)} />
        </label>
        <div className="field">
          <span className="field-label">拍摄顺序</span>
          <span className="field-hint">每行一段，按拍摄顺序写，不要写成完整口播</span>
          <div className="step-list">
            {steps.map((step, index) => (
              <div className="step-row" key={index}>
                <span className="step-index">{index + 1}</span>
                <input value={step} onChange={(event) => updateStep(index, event.target.value)} />
                {steps.length > 1 ? (
                  <button className="text-button" type="button" onClick={() => removeStep(index)}>
                    删除
                  </button>
                ) : null}
              </div>
            ))}
          </div>
          <button className="secondary step-add" type="button" onClick={() => setStructure([...steps, ""].join("\n"))}>
            加一段
          </button>
        </div>
        <label className="field">
          <span className="field-label">这条内容回应什么</span>
          <span className="field-hint">观众评论里反复出现、下一条要接住的需求</span>
          <input value={targetNeed} onChange={(event) => setTargetNeed(event.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">发出后看什么</span>
          <span className="field-hint">用哪类新评论判断这条有没有打中</span>
          <input value={validation} onChange={(event) => setValidation(event.target.value)} />
        </label>
      </section>

      {card.claims.length ? (
        <section className="opp-section">
          <h3>评论里能支撑什么</h3>
          {card.claims.map((claim, index) => (
            <div className="claim" key={`${card.themeId}-${index}`}>
              <div className="claim-head">
                <span className="badge">{claimText(claim)}</span>
                <span className="quiet">{claimHint(claim)}</span>
              </div>
              <label className="field">
                <span className="field-label">说法</span>
                <textarea
                  rows={2}
                  value={claims[index] ?? ""}
                  onChange={(event) => setClaims(claims.map((text, item) => (item === index ? event.target.value : text)))}
                />
              </label>
              {claim.excerpts.map((excerpt) => (
                <blockquote className="excerpt" key={excerpt.commentId}>
                  <p>{excerpt.text}</p>
                  <p className="quiet">来自评论 {excerpt.commentId}</p>
                </blockquote>
              ))}
              {claim.commentIds.length ? (
                <details className="id-fold">
                  <summary>引用了 {claim.commentIds.length} 条评论</summary>
                  <p className="quiet">{claim.commentIds.join("、")}</p>
                </details>
              ) : null}
            </div>
          ))}
        </section>
      ) : null}

      {card.reason ? (
        <p className="opp-reason">
          <span className="quiet">为什么排在第 {card.rank}</span>
          {card.reason}
        </p>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
      <div className="card-actions">
        <button className="primary" type="button" disabled={pending} onClick={() => void save()}>
          {pending ? "保存中" : "保存修改"}
        </button>
        <button className="secondary" type="button" disabled={pending} onClick={() => void save(!card.favorite)}>
          {card.favorite ? "取消收藏" : "收藏"}
        </button>
        <button className="secondary" type="button" onClick={() => onOpenComments(card.themeId)}>
          查看评论
        </button>
      </div>
    </article>
  )
}

const FEASIBILITY = ["", "很难做", "不太好做", "中等", "比较好做", "很好做"]
const COST = ["", "很轻", "偏轻", "中等", "偏重", "很重"]

function scaleText(value: number, words: string[]) {
  return words[value] || String(value)
}

function claimHint(claim: Claim) {
  if (claim.downgraded) return "证据不够，已改成更弱的说法"
  if (claim.kind === "fact") return "这些评论里已经出现的意思"
  if (claim.kind === "hypothesis") return "评论还没证明，是一种拍法假设"
  return "根据这些评论做出的判断"
}

function Stat({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="opp-stat">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  )
}

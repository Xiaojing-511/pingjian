// 评见挂进 DSH 的全部工具。execute 不在这里做分析，只带进程令牌回调本机 FastAPI。
import { defineTool } from "@deepseek-ai/dsh-tools"

export const name = "pingjian-tools"
export const inject = ["tools"]

const TOOLS = [
  ["inspect_import", "第一步。核对已导入的评论。只调用一次，参数只有 taskId。"],
  ["classify_comments", "第二步。做视频定位，并给评论打四个标签。只调用一次。"],
  [
    "reclassify_other",
    "分类之后可选。只调用一次，参数只有 taskId。只有摘要写明「可以考虑补分类」，并且样本里还有成片的具体内容时才调用。这一步让模型自己归纳最多 3 个新类别，不要写回四个预设标签。闲聊、捧场、表情不要调用。调用后继续 cluster_topics。",
  ],
  ["cluster_topics", "第三步。把每个标签下的高赞样本收成主题。只调用一次。"],
  ["rank_opportunities", "第四步。让服务端计算排序分。只调用一次。"],
  ["draft_actions", "第五步。为分数前 8 的主题写选题。只调用一次。"],
  ["commit_report", "第六步。校验证据并提交报告。只调用一次。失败就停止。"],
]

function endpoint(toolName) {
  const base = process.env.PINGJIAN_INTERNAL_BASE || "http://127.0.0.1:8787"
  return `${base}/internal/tools/${toolName}`
}

export function apply(ctx) {
  for (const [toolName, description] of TOOLS) {
    ctx.tools.register(
      defineTool({
        name: toolName,
        description,
        parameters: {
          taskId: { type: "string", required: true, description: "评见任务 ID" },
        },
        output: {
          schema: { type: "string" },
          render: (_args, value) => [{ type: "text", text: value }],
        },
        async execute(args) {
          const token = process.env.PINGJIAN_TOOL_TOKEN || ""
          const response = await fetch(endpoint(toolName), {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "x-pingjian-tool-token": token,
            },
            body: JSON.stringify({ taskId: args.taskId }),
          })
          const text = await response.text()
          if (!response.ok) {
            throw new Error(text.slice(0, 500))
          }
          return text.slice(0, 1600)
        },
      }),
    )
  }
}

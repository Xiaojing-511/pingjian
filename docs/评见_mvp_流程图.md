# 评见 MVP 流程图

实现以 [评见_mvp_final.plan.md](评见_mvp_final.plan.md) 为准。下面按真实分支画出主路径、导入、分析、模型重试、报告校验和事件推送。蓝色是 DeepSeek 调用，红色是失败或拒绝，绿色是落库或完成。

## 1. 主路径

```mermaid
flowchart TD
  open(["打开评见"]) --> logged{"已登录"}
  logged -->|否| card["登录或注册"]
  card --> uid["解析 uid"]
  uid --> shell["进入工作台"]
  logged -->|是| shell
  shell --> act{"选择动作"}
  act -->|导入| wizard["导入向导"]
  act -->|打开报告| board["查看报告三区"]
  act -->|删除| ask{"确认删除"}
  ask -->|否| shell
  ask -->|是| wipe["级联删除该视频"]
  wipe --> shell
  wizard --> accepted{"导入被接受"}
  accepted -->|否| shell
  accepted -->|是| go{"开始分析"}
  go -->|否| shell
  go -->|是| job["进入分析队列"]
  job --> outcome{"分析成功"}
  outcome -->|否| again{"重试该任务"}
  again -->|是| job
  again -->|否| shell
  outcome -->|是| board

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef ok fill:#CDF4D3,stroke:#66D575
  class wipe,again fail
  class board,shell ok
```

删除按 `uid` 和 `videoId` 清掉评论、任务、事件、报告和原始文件。失败任务可以重试，仍使用已导入评论。成功后没有重新分析入口。

## 2. 导入与清洗

浏览器可以先做列映射和样例预览。提交后只认服务端重新解析的结果。

```mermaid
flowchart TD
  submit["确认导入"] --> keys{"密钥齐全"}
  keys -->|否| keyErr["返回缺项错误"]
  keys -->|是| reparse["服务端重新解析"]
  reparse --> videoId["解析视频 ID"]
  videoId --> exists{"该用户已有此视频"}
  exists -->|是| reject["拒绝并提示先删除"]
  exists -->|否| row["取下一行"]
  row --> empty{"内容为空"}
  empty -->|是| cntEmpty["计入空内容"]
  empty -->|否| level{"是一级评论"}
  level -->|否| cntLevel["计入非一级"]
  level -->|是| pid{"有平台评论 ID"}
  pid -->|否| cntPid["计入缺少评论 ID"]
  pid -->|是| dup{"该评论 ID 已保留"}
  dup -->|否| kept["保留该行"]
  dup -->|是| higher{"点赞可比且更高"}
  higher -->|是| replace["替换已保留行"]
  higher -->|否| first["留下先出现的一行"]
  kept --> info{"低信息量"}
  replace --> info
  first --> info
  info -->|是| mark["标记且不发给模型"]
  info -->|否| valid["计入有效评论"]
  cntEmpty --> more{"还有行"}
  cntLevel --> more
  cntPid --> more
  mark --> more
  valid --> more
  more -->|有| row
  more -->|无| persist[("写入视频、评论和原始文件")]
  persist --> summary["展示清洗摘要"]

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef ok fill:#CDF4D3,stroke:#66D575
  classDef store fill:#FFECBD,stroke:#FFC943
  class keyErr,reject fail
  class summary ok
  class persist store
```

摘要分开计数：原始记录、有效评论、重复、空内容、非一级、低信息量、缺少评论 ID。低信息量评论保留，标签为空，计入未归类。

## 3. 分析队列

同一进程同时只跑一个分析。思考文本在写入事件前丢弃。

```mermaid
flowchart TD
  create["创建任务"] --> queued["状态 queued"]
  queued --> free{"进程空闲"}
  free -->|否| queued
  free -->|是| run["状态 started"]
  run --> tools["六个必经工具按序执行"]
  tools --> stopped{"某步失败"}
  stopped -->|是| failed["状态 failed"]
  failed --> retry{"用户重试"}
  retry -->|是| queued
  retry -->|否| halt(["停在失败阶段"])
  stopped -->|否| done(["状态 completed"])

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef ok fill:#CDF4D3,stroke:#66D575
  class failed,halt fail
  class done ok
```

## 4. 工具链

必经顺序：`inspect_import`、`classify_comments`、`cluster_topics`、`rank_opportunities`、`draft_actions`、`commit_report`。分类和聚类之间可以插入一次 `reclassify_other`。`inspect_import` 和 `commit_report` 不调用模型。图中的 DeepSeek 节点都走第 5 节的重试规则。

```mermaid
flowchart TD
  inspect["inspect_import"] --> skipLow["排除低信息量"]
  skipLow --> posCall["定位：调用 DeepSeek"]
  posCall --> posOk{"定位成功"}
  posOk -->|否| failed["任务失败"]
  posOk -->|是| nextBatch["取下一批评论"]
  nextBatch --> hasBatch{"还有实质评论"}
  hasBatch -->|是| clsCall["分类 40 条"]
  clsCall --> clsOk{"分类成功"}
  clsOk -->|否| failed
  clsOk -->|是| nextBatch
  hasBatch -->|否| gate{"未打标签不少于 10 条且达到一半"}
  gate -->|否| nextLabel["取下一个标签"]
  gate -->|是| agent{"编排员看样本"}
  agent -->|闲聊、捧场或表情| nextLabel
  agent -->|还有成片具体内容| reclassify["补分类：最多 3 个新类别"]
  reclassify --> reOk{"补分类成功"}
  reOk -->|否| failed
  reOk -->|是| nextLabel
  nextLabel --> hasLabel{"还有待聚类标签"}
  hasLabel -->|是| cluCall["聚类高赞样本"]
  cluCall --> cluOk{"聚类成功"}
  cluOk -->|否| failed
  cluOk -->|是| keep["留下不少于 2 条的主题"]
  keep --> nextLabel
  hasLabel -->|否| hasTheme{"有有效主题"}
  hasTheme -->|否| skip["跳过排序和选题"]
  hasTheme -->|是| prep["计算样本 count"]
  prep --> rankCall["评估可行性与成本"]
  rankCall --> rankOk{"评估成功"}
  rankOk -->|否| failed
  rankOk -->|是| top8["按 score 取前 8"]
  top8 --> draftCall["生成选题建议"]
  draftCall --> draftOk{"选题成功"}
  draftOk -->|否| failed
  draftOk -->|是| commit["commit_report"]
  skip --> commit

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef model fill:#C2E5FF,stroke:#3DADFF
  classDef ok fill:#CDF4D3,stroke:#66D575
  classDef warn fill:#FFECBD,stroke:#FFC943
  class failed fail
  class posCall,clsCall,reclassify,cluCall,rankCall,draftCall model
  class commit ok
  class agent,skip warn
```

定位每个视频 1 次。输入是标题、话题标签和点赞前 30 条；文件里有视频描述则一并传入。

分类只送给有实质内容的评论，每批 40 条。`groupKey` 只在标签含 `need` 或 `question` 时保留，为 2 到 8 个汉字。全部批次结束后，服务端统计未打标签的有效评论。不少于 10 条且达到有效评论的一半时，摘要写「可以考虑补分类」，并附上点赞最高的 12 条样本。否则写「不要补分类」。

补分类最多一次。编排员先看样本：大多是闲聊、捧场或表情就跳过；还有成片具体内容才调用。服务端在未达条件、重复调用或聚类已开始时拒绝，且不调用模型，任务继续。真正调用时只送未打标签评论里点赞最高的 80 条。模型自己归纳最多 3 个新类别，不写回四个预设标签。不足 2 条的类别丢掉，留下的以 `other` 并入后面的主题。

聚类每个够 2 条的标签 1 次，最多 4 次。每次只送该标签点赞最高的 60 条。主题条数等于校验后的样本归属数，并另存该标签全量条数。全量条数只展示，不进入 `score`。没有任何主题时，排序和选题跳过，报告仍提交，并写明已跳过。

`countNorm = min(count, 20) / 20`。`evidence` 是该主题里成功回填摘录的评论占比。`score = round(100 * countNorm * evidence * feasibility / cost)`。`count` 和 `evidence` 是事实；可行性、成本和原因是推断。

## 5. 单次 DeepSeek 请求

```mermaid
flowchart TD
  send["POST chat completions"] --> check{"JSON 完整且 id 齐全"}
  check -->|是| back(["交回当前工具"])
  check -->|否| tried{"已经重试过"}
  tried -->|否| send
  tried -->|是| giveUp(["记下阶段和缺失 id"])

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef ok fill:#CDF4D3,stroke:#66D575
  classDef model fill:#C2E5FF,stroke:#3DADFF
  class send model
  class back ok
  class giveUp fail
```

模型名取 `DEEPSEEK_MODEL`，缺省 `deepseek-flash`。非流式，JSON 模式，`temperature` 0.2，超时 180 秒，关闭 thinking。评论只传 `id`、`likes` 和截断到 180 字的正文，不传昵称。正文按不可信数据处理。提示词版本记为 `mvp-5`。补分类同样走这条请求，`max_tokens` 4096。

## 6. 报告校验与使用

模型不写摘录。服务端从全文截取连续的、不超过 40 字的片段。主题条数以校验后的评论 ID 数为准。

```mermaid
flowchart TD
  commit["commit_report"] --> fact["检查下一条 fact"]
  fact --> hasFact{"还有 fact"}
  hasFact -->|否| anyTheme{"还有有效主题"}
  hasFact -->|是| owned{"ID 属于该主题"}
  owned -->|否| down["改为推断并去掉 ID"]
  owned -->|是| clip["截取连续摘录"]
  clip --> fact
  down --> mark["界面标已降级"]
  mark --> fact
  anyTheme -->|否| limited["保存定位和分类，并写跳过原因"]
  anyTheme -->|是| save[("保存报告")]
  limited --> save
  save --> overview["总览"]
  save --> chance["内容机会"]
  save --> detail["评论明细"]
  chance --> edit["改文字或收藏"]
  chance --> detail
  overview --> detail
  save --> exportMd["导出 Markdown"]
  save --> exportCsv["导出 CSV"]
  save --> freeze["成功后不可重跑"]

  classDef fail fill:#FFCDC2,stroke:#FF7556
  classDef ok fill:#CDF4D3,stroke:#66D575
  classDef store fill:#FFECBD,stroke:#FFC943
  classDef warn fill:#FFECBD,stroke:#FFC943
  class save,limited store
  class down,mark warn
  class overview,chance,detail ok
```

事实 ID 还必须属于当前用户和当前视频。可改的是选题、结构、目标需求、验证方式和主张文字。计数、评论 ID 和摘录只读。导出使用已保存、且已含用户编辑的报告，不带密钥、其他用户数据和内部事件。

总览展示原始记录、有效评论、主题数、提问数、消极反馈数和未归类。四个标签的条数分开显示。补分类形成的类别放在「其他」。评论明细可按关键词、标签、主题、是否被引用筛选。

## 7. 事件推送

```mermaid
flowchart LR
  step["工具开始或结束"] --> drop["丢弃思考文本"]
  drop --> ev[("pingjian_events")]
  ev --> sse["带 Bearer 的 fetch"]
  refresh["页面刷新"] --> read["读取任务和事件"]
  sse --> page["总览显示阶段"]
  read --> page

  classDef store fill:#FFECBD,stroke:#FFC943
  classDef ok fill:#CDF4D3,stroke:#66D575
  class ev store
  class page ok
```

事件类型有 `queued`、`started`、`stage_started`、`stage_completed`、`summary`、`completed`、`failed`。失败步骤保持失败状态。

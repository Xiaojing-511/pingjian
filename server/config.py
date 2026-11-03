"""进程配置。密钥只从环境变量读取，不写进代码。"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

PROMPT_VERSION = "mvp-5"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_ENV_ID = "cloud1-4grqnt5f5b5d8f20"

# 分析固定走这六个工具，顺序不能换。前一个和最后一个不调用模型。
TOOL_ORDER = (
    "inspect_import",
    "classify_comments",
    "cluster_topics",
    "rank_opportunities",
    "draft_actions",
    "commit_report",
)

# 不在必经顺序里。分类之后，编排员最多调用一次。
OPTIONAL_TOOLS = ("reclassify_other",)


@dataclass(frozen=True)
class Settings:
    cloudbase_env_id: str
    cloudbase_api_key: str
    deepseek_api_key: str
    deepseek_model: str
    dsh_home: str
    # 容器内 DSH 插件回调本进程时携带的随机令牌，每次启动重新生成。
    tool_token: str
    internal_base: str

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.cloudbase_env_id:
            missing.append("CLOUDBASE_ENV_ID")
        if not self.cloudbase_api_key:
            missing.append("CLOUDBASE_API_KEY")
        if not self.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")
        return missing


def load_settings() -> Settings:
    # 模型名可以换，但缺省必须是 deepseek-flash，避免悄悄切到别的模型。
    model = os.environ.get("DEEPSEEK_MODEL", "").strip() or DEFAULT_MODEL
    token = os.environ.get("PINGJIAN_TOOL_TOKEN", "").strip() or secrets.token_urlsafe(32)
    return Settings(
        cloudbase_env_id=os.environ.get("CLOUDBASE_ENV_ID", "").strip() or DEFAULT_ENV_ID,
        cloudbase_api_key=os.environ.get("CLOUDBASE_API_KEY", "").strip(),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        deepseek_model=model,
        dsh_home=os.environ.get("DSH_HOME", "").strip() or "/tmp/pingjian-dsh",
        tool_token=token,
        internal_base=os.environ.get("PINGJIAN_INTERNAL_BASE", "").strip() or "http://127.0.0.1:8787",
    )

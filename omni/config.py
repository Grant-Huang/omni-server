"""Runtime configuration. Every default here is a measured value from workforce, not a
guess -- see the individual comments."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Verified working against the workspace host on 2026-08-23. Flash rather than Plus:
# 百炼 positions Flash as the default for production, and it is both cheaper and lower
# latency, which is what a conversational budget cares about.
DEFAULT_REALTIME_MODEL = "qwen3.5-omni-flash-realtime"
# Not Chelsie -- that voice is not accepted by this model ("Voice 'Chelsie' is not
# supported."). Voice lists are not portable across model generations.
DEFAULT_VOICE = "Ethan"
# qwen-turbo, not qwen3.5-flash: workforce measured 0.9-4.3s versus 17-35s for the same
# short-completion work on the same endpoint. The sidecar runs on every turn, so this
# gap is the difference between a usable lookup and one that always arrives too late.
DEFAULT_TEXT_MODEL = "qwen-turbo"

BASE_INSTRUCTIONS = """你是一个语音助手，正在和用户实时语音对话。

说话方式：
- 像日常聊天一样自然口语化，不要用书面语。
- 不要用任何视觉格式：不用列表符号、编号、加粗，也不要读网址或代码。

回答长度：
- 查询类（单一事实、确认性问题）：1-3 句话说完。
- 列举类（日程、待办）：一口气最多说 3 条，说完问一句还要不要继续。
- 分析解释类：可以详细，但先说一句路线图，再分段说，给用户留插话的空当。

背景信息的使用：
- 背景信息里有相关内容就用自己的话自然带出来，不要逐字复述，也不要提「背景信息」这个说法。
- 需要具体记录但背景信息里没有的，诚实说你没有这方面的记录，不要编。
- 如果问题需要查记录，而你手上还没有，就先简短说一句你去看看，然后停下来等——**不要猜一个答案**。
  查询结果会稍后补进来。
- 常识性、闲聊性的问题正常回答，不用强调「没有记录」。"""


@dataclass
class Config:
    api_key: str = ""
    workspace_id: str = ""
    realtime_model: str = DEFAULT_REALTIME_MODEL
    text_model: str = DEFAULT_TEXT_MODEL
    voice: str = DEFAULT_VOICE
    host: str = "127.0.0.1"
    port: int = 8770
    user_scope: str = "user:local"   # v0 is single-user; identity lands in phase 2

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = env if env is not None else os.environ
        return cls(
            api_key=env.get("QWEN_API_KEY", ""),
            workspace_id=env.get("QWEN_WORKSPACE_ID", ""),
            realtime_model=env.get("QWEN_MODEL", DEFAULT_REALTIME_MODEL),
            text_model=env.get("QWEN_TEXT_MODEL", DEFAULT_TEXT_MODEL),
            voice=env.get("QWEN_VOICE", DEFAULT_VOICE),
            host=env.get("HOST", "127.0.0.1"),
            port=int(env.get("PORT", "8770")),
            user_scope=env.get("OMNI_USER_SCOPE", "user:local"),
        )

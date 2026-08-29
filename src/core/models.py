import os
import time
from typing import Optional, List, Dict, Any
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import dotenv

print(">>> 正在加载 models.py...")
dotenv.load_dotenv()

class LLMClient:
    """
    极简 LLM 网关，负责统一 API 调用，并内置重试机制。
    参考 Waku 的 60 行适配器，但我们先只支持 OpenAI 接口。
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("MODEL_NAME", "gpt-4o-mini")
        self.temperature = temperature

        if not self.api_key:
            raise ValueError("OpenAI API Key 未设置，请检查 .env 文件")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(Exception),  # 实际应细化，但入门先全量重试
        reraise=True,
    )
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        **kwargs,
    ) -> str:
        """
        发送对话，返回模型回复的文本。
        """
        try:
            response = self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature or self.temperature,
                **kwargs,
            )
            return response.choices[0].message.content
        except Exception as e:
            # tenacity 会自动重试，但我们可以记录错误日志（这里先忽略）
            raise e
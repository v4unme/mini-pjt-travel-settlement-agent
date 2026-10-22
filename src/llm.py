"""llm.py - Bedrock Claude 모델 생성 (그래프·도구·평가가 같은 설정을 쓰도록 한 곳에 둔다)"""
import os

from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse

load_dotenv()

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def build_llm():
    """Bedrock Claude 모델을 만든다. 모델 ID·리전은 .env 에서 읽는다."""
    return ChatBedrockConverse(
        model=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        temperature=0,
    )

"""
core/llm.py

LLM Client dùng chung cho toàn bộ hệ thống.

Hỗ trợ:
- Groq
- Anthropic
- OpenAI
- OpenRouter

Features:
- invoke
- ainvoke
- structured_invoke
- retry
- tool calling
"""

from typing import Optional, Type, List, Union
import logging
import os
import sys
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pydantic import BaseModel
from langchain.tools import BaseTool
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate

from core.configs import env_config


logger = logging.getLogger(__name__)


class LLMClient:

    def __init__(
        self,
        model: Optional[str] = None,
        api_provider: Optional[str] = None,
    ):
        self.model = model or env_config.model
        self.api_provider = (
            api_provider or env_config.llm_provider
        )
        self._llm = self._initialize_llm()

    def _initialize_llm(self):

        provider = self.api_provider

        if provider == "groq":
            from langchain_groq import ChatGroq

            if not env_config.groq_api_key:
                raise ValueError("Missing GROQ_API_KEY")

        return ChatGroq(model=self.model, groq_api_key=env_config.groq_api_key,
                        reasoning_effort="low")

        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            if not env_config.anthropic_api_key:
                raise ValueError("Missing ANTHROPIC_API_KEY")

            return ChatAnthropic(
                model=self.model,
                api_key=env_config.anthropic_api_key,
                max_tokens=4096,
            )

        if provider == "openai":
            from langchain_openai import ChatOpenAI

            if not env_config.openai_api_key:
                raise ValueError("Missing OPENAI_API_KEY")

            return ChatOpenAI(
                model=self.model,
                api_key=env_config.openai_api_key,
            )

        if provider == "openrouter":
            from langchain_openai import ChatOpenAI

            if not env_config.openrouter_api_key:
                raise ValueError("Missing OPENROUTER_API_KEY")

            return ChatOpenAI(
                model=self.model,
                api_key=env_config.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
            )

        raise ValueError(f"Unsupported provider: {provider}")

    def _build_llm(
        self,
        temperature: float = 0,
        max_tokens: int = 4096,
        tools: Optional[List[BaseTool]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        structured_method: Optional[str] = None,
    ):

        llm = self._llm.bind(
            temperature=temperature,
            max_tokens=max_tokens,
        )

        if tools:
            llm = llm.bind_tools(tools)

        if output_schema:
            kwargs = {}
            if structured_method:
                kwargs["method"] = structured_method

            llm = llm.with_structured_output(
                output_schema,
                **kwargs,
            )

        return llm

    def invoke(
        self,
        text: str,
        temperature: float = 0,
        max_tokens: int = 4096,
    ):

        llm = self._build_llm(
            temperature=temperature,
            max_tokens=max_tokens,
        )

        response = llm.invoke(text)

        if hasattr(response, "content"):
            return response.content

        return response

    async def ainvoke(
        self,
        text: str,
        temperature: float = 0,
        max_tokens: int = 4096,
    ):

        llm = self._build_llm(
            temperature=temperature,
            max_tokens=max_tokens,
        )

        response = await llm.ainvoke(text)

        if hasattr(response, "content"):
            return response.content

        return response

    # def structured_invoke(
    #     self,
    #     text: str,
    #     output_schema: Type[BaseModel],
    #     temperature: float = 0,
    #     max_tokens: int = 4096,
    #     structured_method: Optional[str] = None,
    # ):

    #     llm = self._build_llm(
    #         temperature=temperature,
    #         max_tokens=max_tokens,
    #         output_schema=output_schema,
    #         structured_method=structured_method,
    #     )

    #     return llm.invoke(text)

    def structured_invoke(
        self,
        text: str,
        output_schema: Type[BaseModel],
        temperature: float = 0,
        max_tokens: int = 4096,
        structured_method: Optional[str] = None,
        max_repair: int = 2,
    ):

        llm = self._build_llm(
            temperature=temperature,
            max_tokens=max_tokens,
            output_schema=output_schema,
            structured_method=structured_method,
        )

        try:
            return llm.invoke(text)

        except Exception as e:

            logger.warning(
                f"Structured output failed: {e}"
            )

            raw = self.invoke(
                text,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            if hasattr(raw, "content"):
                raw = raw.content

            for _ in range(max_repair):

                try:

                    fixed = self.repair_output(
                        raw_output=raw,
                        output_schema=output_schema,
                    )

                    if hasattr(fixed, "content"):
                        fixed = fixed.content

                    return output_schema.model_validate_json(
                        fixed
                    )

                except Exception as repair_error:

                    logger.warning(
                        f"Repair failed: {repair_error}"
                    )

                    if "fixed" in locals():

                        if hasattr(fixed, "content"):
                            raw = fixed.content
                        else:
                            raw = fixed

            raise ValueError(
                "Cannot repair structured output"
            )

    async def structured_ainvoke(
        self,
        text: str,
        output_schema: Type[BaseModel],
        temperature: float = 0,
        max_tokens: int = 4096,
        structured_method: Optional[str] = None,
        max_repair: int = 2,
    ):

        llm = self._build_llm(
            temperature=temperature,
            max_tokens=max_tokens,
            output_schema=output_schema,
            structured_method=structured_method,
        )

        try:
            return await llm.ainvoke(text)

        except Exception as e:

            logger.warning(
                f"Structured output failed: {e}"
            )

            raw = await self.ainvoke(
                text,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            if hasattr(raw, "content"):
                raw = raw.content

            for _ in range(max_repair):

                try:

                    fixed = await self.arepair_output(
                        raw_output=raw,
                        output_schema=output_schema,
                    )

                    if hasattr(fixed, "content"):
                        fixed = fixed.content

                    return output_schema.model_validate_json(
                        fixed
                    )

                except Exception as repair_error:

                    logger.warning(
                        f"Repair failed: {repair_error}"
                    )

                    if "fixed" in locals():

                        if hasattr(fixed, "content"):
                            raw = fixed.content
                        else:
                            raw = fixed

            raise ValueError(
                "Cannot repair structured output"
            )

    def repair_output(
        self,
        raw_output: str,
        output_schema: Type[BaseModel],
    ):

        schema = output_schema.model_json_schema()

        prompt = f"""
                Bạn là JSON Repair Agent.

                Schema:
                {schema}

                Output lỗi:
                {raw_output}

                Hãy sửa output để đúng schema.

                Chỉ trả về JSON hợp lệ.
                """

        return self.invoke(
            prompt,
            temperature=0,
            max_tokens=4096,
        )


    async def arepair_output(
        self,
        raw_output: str,
        output_schema: Type[BaseModel],
    ):

        schema = output_schema.model_json_schema()

        prompt = f"""
                Bạn là JSON Repair Agent.

                Schema:
                {schema}

                Output lỗi:
                {raw_output}

                Hãy sửa output để đúng schema.

                Chỉ trả về JSON hợp lệ.
                """

        return await self.ainvoke(
            prompt,
            temperature=0,
            max_tokens=4096,
        )

    def invoke_with_retries(
        self,
        prompt: ChatPromptTemplate,
        max_tokens: int = 4096,
        temperature: float = 0,
        tools: Optional[List[BaseTool]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        retries: int = 3,
        structured_method: Optional[str] = None,
    ):

        llm = self._build_llm(
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            output_schema=output_schema,
            structured_method=structured_method,
        )

        for attempt in range(retries):

            try:
                chain = prompt | llm
                return chain.invoke({})

            except Exception as e:

                logger.error(
                    f"Attempt {attempt + 1}/{retries} failed: {e}"
                )

                if attempt == retries - 1:
                    raise

    async def ainvoke_with_retries(
        self,
        prompt: Union[ChatPromptTemplate, List[BaseMessage]],
        max_tokens: int = 4096,
        temperature: float = 0,
        tools: Optional[List[BaseTool]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        retries: int = 3,
        retry_temperature_step: float = 0.0,
        structured_method: Optional[str] = None,
    ):

        for attempt in range(retries):

            current_temperature = max(
                0,
                temperature + attempt * retry_temperature_step,
            )

            llm = self._build_llm(
                temperature=current_temperature,
                max_tokens=max_tokens,
                tools=tools,
                output_schema=output_schema,
                structured_method=structured_method,
            )

            try:

                if isinstance(prompt, list):
                    return await llm.ainvoke(prompt)

                chain = prompt | llm
                return await chain.ainvoke({})

            except Exception as e:

                logger.error(
                    f"Attempt {attempt + 1}/{retries} failed "
                    f"(temperature={current_temperature}): {e}"
                )

                if attempt == retries - 1:
                    raise



# ============================================================
# Quick Test
# python core/llm.py
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("LLM TEST")
    print("=" * 60)

    # print(f"Provider : {env_config.llm_provider}")
    # print(f"Model    : {env_config.model}")
    # print(f"API Key  : {env_config.anthropic_api_key}")
    # print(repr(env_config.anthropic_api_key))
    print("ok letsgo")

    try:

        llm = LLMClient()

        query ="""
        2022 ai là tổng thống nước mỹ
        """

        response = llm.invoke(
            text=query,
            temperature=0.4
        )

        print("Response:")
        print("-" * 60)
        print(response)
        print("-" * 60)

    except Exception as e:
        print(f"ERROR: {e}")
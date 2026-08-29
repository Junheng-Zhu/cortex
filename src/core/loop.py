import json
import uuid
import time          # 新增这一行
from .models import LLMClient
from ..tools.file_tools import TOOL_REGISTRY
from ..memory.gated_retriever import (
    should_retrieve,
    search_memory,
    remember_interaction,
)
from ..ops.tracer import log_event



def run_loop(client: LLMClient):

    session_id = str(uuid.uuid4())[:8]  # 短 ID，便于区分
    print(f"🔍 会话 ID: {session_id} (Dashboard 将按此归类)")
    log_event(session_id, "system", "Agent 启动")

    """
    带有工具调用能力的 Agent 循环。
    """
    print("Cortex 已启动（工具模式），输入 'exit' 退出。")

    system_prompt = """你是一个名叫 Cortex 的个人助手，专门管理用户的笔记文件。
    【工具使用规则】
    - 如果用户想查看**当前**有哪些笔记文件，调用 list_notes。
    - 如果用户想查看**当前**某个文件的内容，调用 read_note。
    【记忆使用规则】
    - 如果上下文中有“【背景记忆】”提供的信息，并且这些信息能够直接回答用户的问题，**请优先使用记忆内容**，不要再重复调用 read_note 或 list_notes。
    - 只有当记忆信息不完整或明显过时时，才去调用工具。
    如果用户的问题与笔记无关，你可以直接回答。
    """

    # 构建工具定义（OpenAI 格式）
    tools = []
    for name, info in TOOL_REGISTRY.items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": info["parameters"],
                },
            }
        )

    history = [{"role": "system", "content": system_prompt}]

    while True:
        user_input = input("\n你: ")
        if user_input.strip().lower() in ("exit", "quit", "q"):
            print("再见！")
            break
        # 1. 将用户输入加入历史
        history.append({"role": "user", "content": user_input})
        log_event(session_id, "user_input", user_input)  # 新增

        # ==================== 图路由：快速通道判断 ====================
        # 规则：如果用户输入很短，且包含闲聊关键词，直接走快速回复，跳过工具和记忆
        """ trivial_keywords = ["你好", "天气", "股票", "心情", "哈哈", "谢谢", "ok", "好的", "今天", "怎么样", "嗯"]
        is_trivial = any(k in user_input for k in trivial_keywords) and len(user_input) < 15

        if is_trivial:
            print("[DEBUG] 🟢 路由判定: 闲聊，走快速通道（跳过工具和记忆）")
            try:
                # 快速通道：不带 tools 参数，不注入记忆，直接调用 LLM
                response = client.client.chat.completions.create(
                    model=client.model,
                    messages=history,  # 只带历史对话，无工具
                    temperature=0.7,
                )
                reply = response.choices[0].message.content
                history.append({"role": "assistant", "content": reply})
                # 闲聊不存入长期记忆，避免污染向量库
                print(f"Cortex: {reply}")
                continue  # 跳过后面所有的门控和工具调用逻辑，进入下一次循环
            except Exception as e:
                print(f"❌ 快速通道失败，降级走深度流程: {e}") """
                # 如果快速通道出错，继续往下走深度流程（不 continue）
        # ============================================================
        
        # ==================== 升级版图路由（多分支） ====================
        # 定义关键词
        is_greeting = any(k in user_input for k in ["你好", "嗨", "喂", "天气", "心情", "哈哈", "谢谢", "辛苦了"])
        is_write = any(k in user_input for k in ["记下", "写个", "创建", "新建", "保存", "添加", "记录一下"])
        is_list_or_read = any(k in user_input for k in ["看看", "列出", "读一下", "显示", "有哪些", "里面有什么", "打开"])
        is_memory_query = any(k in user_input for k in ["之前", "刚才", "上次", "回忆", "以前", "我说过"])

        # 1. 🟢 闲聊分支（最短路径）
        if is_greeting and len(user_input) < 15:
            print("[DEBUG] 🟢 路由: 闲聊分支")
            try:
                # 注意：这里为了保留上下文，我们还是调用 LLM，但不带 tools，不传 memory
                response = client.client.chat.completions.create(
                    model=client.model,
                    messages=history,  # 只有历史记录，没工具
                    temperature=0.7,
                )
                reply = response.choices[0].message.content
                history.append({"role": "assistant", "content": reply})
                print(f"Cortex: {reply}")
                continue  # 结束本次循环，回到开头等待输入
            except Exception as e:
                print(f"❌ 闲聊失败，降级: {e}")
                # 降级继续往下走

        # 2. 🔵 只读分支（操作当前文件，不需要回忆历史）
        if is_list_or_read:
            print("[DEBUG] 🔵 路由: 只读文件分支（跳过记忆检索，直接调用工具）")
            # 直接跳过 should_retrieve，进入后面的工具调用逻辑
            # 注意：这里我们不设置 should_retrieve 的返回值，而是直接跳到工具调用。
            # 为了实现这一点，我们把下面原本的 "if should_retrieve" 改成条件判断。
            # 为了不改太多代码，我们在这里设置一个标志变量，让后面的门控失效。
            skip_memory = True
        elif is_memory_query:
            print("[DEBUG] 🟡 路由: 记忆检索分支（优先回忆，不主动调用工具）")
            skip_memory = False
        elif is_write:
            print("[DEBUG] 🟣 路由: 写入分支（操作后强制记忆）")
            skip_memory = True
        else:
            # 默认走原有的混合深度流程
            print("[DEBUG] ⚪ 路由: 默认深度流程")
            skip_memory = False

        # ============================================================
        # 后续逻辑改造：利用 skip_memory 标志控制门控

        # 2. 【新增】检索门控记忆
        """  relevant_memories = []
        if should_retrieve(user_input):
            print("[DEBUG] 门控通过，正在检索记忆...")
            relevant_memories = search_memory(user_input, top_k=2)
            if relevant_memories:
                # 将检索到的记忆注入上下文（放在 System Prompt 后面，用户消息前面）
                memory_context = "\n".join(
                    [f"[过往记忆] {m}" for m in relevant_memories]
                )
                # 插入一条临时的系统消息来承载记忆（但为了不污染历史，直接修改最后一条用户消息）
                # 更稳健的做法：在调用 LLM 时，动态拼接 memory 到 messages 中。
                # 我们采用简单方式：将记忆内容附加到当前用户消息之前。
                history[-1][
                    "content"
                ] = f"【背景记忆】\n{memory_context}\n\n当前用户问题: {user_input}"
                print("[DEBUG] 已注入记忆到上下文")
        else:
            print("[DEBUG] 门控未通过，跳过检索") """

        """ # 2. 【升级】检索门控记忆（受路由标志控制）
        relevant_memories = []
        # 如果是只读分支（🔵）或写入分支（🟣），强制跳过记忆检索，避免干扰
        if is_list_or_read or is_write:
            print("[DEBUG] 路由标志: 强制跳过记忆检索")
            pass  # 不检索，relevant_memories 保持空列表
        elif should_retrieve(user_input):  # 默认走门控判断
            print("[DEBUG] 门控通过，正在检索记忆...")
            relevant_memories = search_memory(user_input, top_k=2)
            if relevant_memories:
                # ... 注入记忆的逻辑（保持不变） ...
                memory_context = "\n".join([f"[过往记忆] {m}" for m in relevant_memories])
                history[-1]["content"] = f"【背景记忆】\n{memory_context}\n\n当前用户问题: {user_input}"
                print("[DEBUG] 已注入记忆到上下文")
        else:
            print("[DEBUG] 门控未通过，跳过检索") """


        # ==================== 门控记忆检索（受路由标志控制） ====================
        relevant_memories = []
        # 如果是只读分支（🔵）或写入分支（🟣），强制跳过记忆检索，避免干扰
        if is_list_or_read or is_write:
            print("[DEBUG] 路由标志: 强制跳过记忆检索")
            log_event(session_id, "gate", "强制跳过记忆检索（路由分支决定）", metadata={"query": user_input, "branch": "read/write"})
            pass
        elif should_retrieve(user_input):
            print("[DEBUG] 门控通过，正在检索记忆...")
            log_event(session_id, "gate", "门控通过，开始检索", metadata={"query": user_input})
            relevant_memories = search_memory(user_input, top_k=2)
            if relevant_memories:
                memory_context = "\n".join([f"[过往记忆] {m}" for m in relevant_memories])
                history[-1]["content"] = f"【背景记忆】\n{memory_context}\n\n当前用户问题: {user_input}"
                print("[DEBUG] 已注入记忆到上下文")
                log_event(session_id, "gate", "记忆注入成功", metadata={"memories_count": len(relevant_memories)})
        else:
            print("[DEBUG] 门控未通过，跳过检索")
            log_event(session_id, "gate", "门控未通过（相似度低于阈值）", metadata={"query": user_input})

        # 3. 调用 LLM（原有逻辑，后面再补上 remember_interaction）
        # 在调用 LLM 前，打印最终送进去的用户消息
        print(f"[DEBUG] 送LLM的用户消息预览: {history[-1]['content'][:100]}...")

        # ----- 第一次 LLM 调用：决定是否调用工具 -----
        try:
            llm_start = time.time()  # 新增

            response = client.client.chat.completions.create(
                model=client.model,
                messages=history,
                tools=tools,
                tool_choice="auto",  # 让模型自己决定是否用工具
                temperature=0.3,  # 降低随机性，确保稳定调用工具
            )

            llm_duration = (time.time() - llm_start) * 1000  # 新增
            # 记录 LLM 调用（包含 Token 消耗）
            usage = response.usage
            log_event(
                session_id,
                "llm_call",
                f"首次推理 (tools={len(tools)})",
                duration_ms=llm_duration,
                tokens_used=usage.total_tokens if usage else 0,
                metadata={"model": client.model, "prompt_tokens": usage.prompt_tokens if usage else 0}
            )
        except Exception as e:
            print(f"❌ 调用 LLM 失败: {e}")
            continue

        msg = response.choices[0].message

        # ----- 处理工具调用 -----
        if msg.tool_calls:
            # 先将助手的原始回复（带工具调用指令）加入历史
            history.append(msg.model_dump())

            for tool_call in msg.tool_calls:
                tool_name = tool_call.function.name

                tool_start = time.time()  # 新增
                tool_args = json.loads(tool_call.function.arguments)

                print(f"🔧 [工具调用] 正在调用工具: {tool_name}")  # 加上这行

                # 从注册表中找到并执行对应的函数
                tool_info = TOOL_REGISTRY.get(tool_name)
                if not tool_info:
                    result = f"错误：未知工具 {tool_name}"
                else:
                    try:
                        result = tool_info["func"](**tool_args)
                    except Exception as e:
                        result = f"工具执行出错: {e}"

                # 在 history.append 前
                result_str = str(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "... (内容过长，已截断)"


                # 将工具执行结果作为一条消息加入历史（role = "tool"）
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    }
                )
                tool_duration = (time.time() - tool_start) * 1000  # 新增
                log_event(
                    session_id,
                    "tool_call",
                    f"{tool_name}({tool_args})",
                    duration_ms=tool_duration,
                    metadata={"result_preview": str(result)[:100]}
                )
            

            # ----- 第二次 LLM 调用：根据工具结果生成最终回复 -----
            try:
                final_response = client.client.chat.completions.create(
                    model=client.model,
                    messages=history,
                    temperature=0.7,
                    max_tokens=1024  # 新增
                )
                final_reply = final_response.choices[0].message.content
                log_event(session_id, "response", final_reply)  # 新增
                history.append({"role": "assistant", "content": final_reply})
                remember_interaction(user_input, final_reply)
                print(f"Cortex: {final_reply}")
            except Exception as e:
                print(f"❌ 生成最终回复失败: {e}")
        else:
            # 没有工具调用，直接回复
            reply = msg.content or "（模型返回空内容）"
            history.append({"role": "assistant", "content": reply})
            remember_interaction(user_input, reply)
            print(f"Cortex: {reply}")

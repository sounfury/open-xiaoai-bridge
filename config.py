"""用户配置：音箱接入、音频参数、唤醒路由及各 AI 后端设置。"""

import asyncio
import socket
import subprocess
import sys
import time
import uuid

import requests


# 每次唤醒生成独立 Session，对话互不干扰
# 适合以下场景：
#   - "提问 → 回答"式交互，不需要 Agent 记住上下文
#   - 长期使用同一 Session 导致 Agent 上下文窗口堆积过长，影响响应质量和速度
# def new_session_key():
#     return f"agent:main:session-{uuid.uuid4().hex[:8]}"


async def before_wakeup(speaker, text, source, app):
    """
    处理收到的用户消息，并决定是否唤醒 AI。

    参数：
        speaker : SpeakerManager，可调用 play/abort_xiaoai/wake_up 等方法
        text    : 识别到的文字内容
        source  : 唤醒来源
                    'kws'    — 本地关键词唤醒（用户说了唤醒词）
                    'xiaoai' — 小爱同学收到用户语音指令
        app     : MainApp 实例，可调用 send_to_openclaw / send_to_openai / send_to_qwenpaw 等方法

    返回值：
        "openclaw" — 进入 OpenClaw 连续对话流程
        "openai"   — 进入 OpenAI 兼容服务连续对话流程（例如 Hermes Agent API Server）
        "qwenpaw"  — 进入 QwenPaw 连续对话流程
        "xiaozhi"  — 进入小智 AI 流程
        None       — 不做额外处理（可在此自行调用 app.send_to_openclaw 等）

    ---
    动态切换 session_key：
        每次进入此函数前，框架会自动将 session_key 重置为配置文件中的默认值。
        如需路由到其他 Agent，在 return "openclaw" 之前调用：
            app.set_openclaw_session_key("agent:main:xxx")
        不调用则自动使用 openclaw.session_key 的默认值，无需手动重置。
    """
    # --- 小爱管家："你好缪斯" → OpenAI 兼容服务（Hermes）连续对话（需 MUSE_ENABLE=1 和 OPENAI_ENABLE=1）---
    # KWS 命中唤醒词，或小爱 ASR 文本以 muse.wake_words 中任一项开头（如"你好缪斯，我今晚想洗澡"、"Hey Muse，开灯"）
    from core.muse import match_muse_text, suppress_native_xiaoai

    if match_muse_text(text):
        if source == "xiaoai":
            await suppress_native_xiaoai(speaker)  # 拦截原生小爱对这句话的处理
        return "openai"

    if source == "kws":
        # --- 示例一：按唤醒词路由到不同 Agent ---
        # AGENT_SESSIONS = {
        #     "龙虾": "agent:assistant:open-xiaoai-bridge",  # 说"你好龙虾" → 路由到 assistant Agent
        #     "小美": "agent:xiaomei:open-xiaoai-bridge",    # 说"你好小美" → 路由到 xiaomei Agent
        #     "管家": "agent:butler:open-xiaoai-bridge",     # 说"你好管家" → 路由到 butler Agent
        # }
        # for keyword, session_key in AGENT_SESSIONS.items():
        #     if keyword in text:
        #         app.set_openclaw_session_key(session_key)
        #         await speaker.play(text=f"{keyword}来了")
        #         return "openclaw"

        # --- 示例二：每次唤醒生成独立 Session ---
        # if "龙虾" in text:
        #     app.set_openclaw_session_key(new_session_key())
        #     await speaker.play(text="龙虾来了")
        #     return "openclaw"

        # --- 示例三：进入 OpenClaw 前播放服务端本地开场白 ---
        # if "龙虾" in text:
        #     await speaker.play(server_file="/path/to/openclaw_intro.wav")
        #     return "openclaw"

        # Route to OpenClaw Agent by wake word
        if "龙虾" in text:
            await speaker.play(text="龙虾来了")
            return "openclaw"

        if "小黑" in text:
            await speaker.play(text="小黑来了")
            return "openai"

        if "小爪" in text:
            await speaker.play(text="小爪来了")
            return "qwenpaw"

        if "小智" in text:
            await speaker.play(text="小智来了")
            return "xiaozhi"

        return None

    if source == "xiaoai":
        # --- 示例四：小爱指令按用户名路由到不同 Session ---
        # if text == "召唤小美":
        #     app.set_openclaw_session_key("agent:xiaomei:open-xiaoai-bridge")
        #     await speaker.abort_xiaoai()
        #     return "openclaw"

        if text == "召唤龙虾":
            await speaker.abort_xiaoai()
            return "openclaw"  # OpenClaw continuous conversation

        if text == "召唤小黑":
            await speaker.abort_xiaoai()
            return "openai"  # OpenAI-compatible service continuous conversation

        if text == "召唤小爪":
            await speaker.abort_xiaoai()
            return "qwenpaw"  # QwenPaw continuous conversation

        if text == "召唤小智":
            await speaker.abort_xiaoai()
            return "xiaozhi"  # XiaoZhi AI

        if "让龙虾" in text:
            await speaker.abort_xiaoai()
            # One-shot: send to OpenClaw and play the reply via TTS
            await app.send_to_openclaw_and_play_reply(text.replace("让龙虾", ""))
            return None  # No further handling by the framework

        if "告诉龙虾" in text:
            await speaker.abort_xiaoai()
            # Fire-and-forget: let the Agent decide when/how to reply
            await app.send_to_openclaw(text.replace("告诉龙虾", ""))
            return None

        if "让小黑" in text:
            await speaker.abort_xiaoai()
            await app.send_to_openai_and_play_reply(text.replace("让小黑", ""))
            return None

        if "让小爪" in text:
            await speaker.abort_xiaoai()
            await app.send_to_qwenpaw_and_play_reply(text.replace("让小爪", ""))
            return None


async def after_wakeup(speaker, source=None, session_key=None):
    """
    退出唤醒状态

    - source: 退出来源
        - 'xiaozhi': 小智对话超时退出
        - 'openclaw': OpenClaw 连续对话退出
        - 'openai': OpenAI 兼容服务连续对话退出
        - 'qwenpaw': QwenPaw 连续对话退出
    - session_key: 当前 OpenClaw/OpenAI/QwenPaw 后端 session_key
        可据此区分是哪个 Agent 退出，例如播放不同的退出提示语
    """
    if source == "openclaw":
        # 示例：退出 OpenClaw 时播放服务端本地结束语
        # await speaker.play(server_file="/path/to/openclaw_bye.wav")

        # 示例：按 agentId 区分退出提示语
        # 所有后端的 session_key 已统一为 agent:<agentId>:<rest> 格式，第二段即 agentId。
        # 仍建议做越界防护，以兼容自定义的非标准 session_key。
        # parts = session_key.split(":") if session_key else []
        # agent_id = parts[1] if len(parts) > 1 else None
        # if agent_id == "assistant":
        #     await speaker.play(text="助手，再见")
        # elif agent_id == "xiaomei":
        #     await speaker.play(text="小美，再见")
        # else:
        #     await speaker.play(text="再见")
        await speaker.play(text="龙虾，再见")
    if source == "openai":
        await speaker.play(text="小黑，再见")
    if source == "qwenpaw":
        await speaker.play(text="小爪，再见")
    if source == "xiaozhi":
        await speaker.play(text="小智，再见")

APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        # MUSE_ENABLE=1 时，生成 keywords.txt 会自动并入 muse.kws_wake_words（无需在这里重复）
        "keywords": [
            "你好小智",
            "小智小智",
            "hi open claw",
            "你好龙虾",
            "龙虾你好",
            "你好小黑",
            "小黑你好",
            "你好小爪",
            "小爪你好",
            "你好缪斯",
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 20,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "kws": {
        # 唤醒词置信度加成（越高越难误触发，越低越灵敏）
        "keywords_score": 2.0,
        # 唤醒词检测阈值（越低越灵敏，越高越难触发）
        "keywords_threshold": 0.2,
        # 单独调整某个唤醒词的阈值 / 加成（键写法同 wakeup.keywords，没列出的用上面的默认值）。
        # 嗨缪斯 / 嘿缪斯 第一个字轻、容易被吃掉，阈值放低；hey / hello muse 会把 "museum" / "amuse" 认成它，阈值提高。
        "keyword_thresholds": {
            "嗨缪斯": 0.02,
            "嘿缪斯": 0.02,
            "hi muse": 0.1,
            "hey muse": 0.25,
            "hello muse": 0.35,
        },
        "keyword_scores": {},
        # 解码时保留的候选数：越大越不容易漏，也越耗 CPU（sherpa 默认 8）
        "max_active_paths": 16,
        # 同时跑几路检测，起点各错开多少 ms，任一路认出即唤醒。模型按约 320 ms 一块处理音频，
        # 唤醒词落在块里的位置会影响能不能认出来；[0] = 只跑一路
        "stream_offsets_ms": [0, 80, 160, 240],
        # VAD 判定开口前的这段音频也补进模型（唤醒词开头的字偏轻，VAD 判定时已经说出一小段）
        "preroll_ms": 300,
        # True：只在 VAD 判定有人说话时才跑唤醒检测（省 CPU）；False：每帧都跑
        "vad_gate": True,
        # 唤醒词检测时的最小静默时长（ms），静默超过该时长则判定为说完
        "min_silence_duration": 480,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    # 外部后端（OpenAI / OpenClaw / QwenPaw）连续对话的轮次衔接
    "conversation": {
        # 唤醒后 / 每轮回答播完后播提示音。有回采时提示音不耽误听：这期间说的话会回放给 VAD
        "start_sound": True,
        "next_turn_sound": True,
        # 回答播完后跳过这么长再开始听（避开播报的最后一点余音），ms
        "tts_end_guard_ms": 150,
        # 唤醒词认出时已经过去一小段，从认出的位置往回这么多开始听（"你好缪斯，开灯"），ms
        "keyword_tail_ms": 100,
        # 没有回采、重新开麦后丢掉开头这么长（可能带着提示音余音），ms
        "mic_start_skip_ms": 150,
    },
    "audio_input": {
        # 进入 VAD/KWS/ASR 前放大麦克风输入，1.0 = 不放大。
        # OwnDing 客户端（S32 >> 8）比旧客户端（S16，相当于 >> 16）响 256 倍，说话时已接近满幅，不要再放大；
        # 旧客户端时这里是 4.0。
        "gain": 1.0,
    },
    "barge_in": {
        # 播报时喊唤醒词（如"你好缪斯"）可以打断，然后直接说新的问题（需要 echo_reference）。
        # 回采不在线（旧客户端 / 连接刚建立）时自动退回"关麦播报"。
        "enabled": True,
        # 让音箱同时发回正在播放的声音（[麦克风, 播放回采] 双声道），bridge 据此消除回声。
        # 需要 OwnDing 版客户端（client-2026.10.09+）；只在 OH2P 上验证过。
        # 环境变量 BARGE_IN_ECHO_REFERENCE=1/0 可临时覆盖。
        "echo_reference": True,
        # 双声道里麦克风声道电平偏低，消除回声后乘这个倍数回到单声道录音的电平（之后再乘 audio_input.gain）
        "echo_mic_gain": 64,
        # 打断后补录：从唤醒词结束前这么多毫秒开始，接上"你好缪斯，明天天气怎么样"里后半句（ms）
        "speech_lookback_ms": 350,
        # 刚播放过声音的那一小段（播放中、停播后 echo_vad_hangover_ms 内），消除后剩下的残留
        # 仍可能被当成说话；这段时间里说话判定用这个更高的阈值（真人说话约 1.0）。None = 不提高
        "echo_vad_threshold": 0.7,
        "echo_vad_hangover_ms": 300,
        # 打断时比较"消除回声后剩下的声音"和"消掉的回声"（dB）。设成数字（如 -17）后，
        # 低于它的唤醒当作音箱自己的声音忽略；None 只记日志。
        "near_end_min_db": None,
        # 回声消除器（core/services/audio/aec.py）
        "echo_canceller": {
            "taps": 8,  # 回声尾巴长度（帧，每帧 16 ms）
            "tau_s": 3.0,  # 回声路径估计的平均时长（秒）
        },
        # OH2P 的 hw:0,3（共享名 Capture）：4 声道 48 kHz，ch0 麦克风，ch3 播放回采
        "capture": {
            "pcm": "Capture",
            "channels": 4,
            "sample_rate": 48000,
            "mic_channel": 0,
            "ref_channel": 3,
            "mic_shift": 14,
            "ref_shift": 16,
        },
    },
    "asr": {
        # 支持 "sense_voice"（默认）、"paraformer"、"fire_red_asr" 或 "doubao"
        "model": "sense_voice",
        # 是否优先使用 INT8 量化模型（仅本地模型生效）
        "int8": True,
        # 可选：显式指定 core/models/ 下的模型目录名（仅本地模型生效）
        # "model_dir": "",
        "doubao": {
            # "standard": 录音文件识别标准版，调用 /submit + /query
            # "flash": 录音文件极速版，调用 /recognize/flash
            "mode": "standard",
            "app_key": "你的 App Key",
            "access_key": "你的 Access Key",
            # 火山 X-Api-Resource-Id：
            # standard 可选：
            #   "volc.bigasr.auc"  - 豆包录音文件识别模型 1.0
            #   "volc.seedasr.auc" - 豆包录音文件识别模型 2.0
            # flash 可选：
            #   "volc.bigasr.auc_turbo" - 录音文件极速版
            "resource_id": "volc.seedasr.auc",
            "language": "",
            "submit_timeout": 10,
            "query_timeout": 10,
            "poll_interval": 0.5,
            "max_wait_seconds": 20,
        },
    },
    "xiaozhi": {
        "OTA_URL": "http://127.0.0.1:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://127.0.0.1:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "DEVICE_ID": "7c:70:db:88:15:d9", #（可选）默认自动生成
        "VERIFICATION_CODE": "", # 首次登陆时，验证码会在这里更新
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,  # 最多连续重新唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        "doubao": {
            # 豆包语音合成 API 配置
            # 文档地址: https://www.volcengine.com/docs/6561/1598757?lang=zh
            # 产品地址: https://www.volcengine.com/docs/6561/1871062
            "app_id": "xxxx",         # 你的 App ID
            "access_key": "xxxxxx",       # 你的 Access Key
            "default_speaker": "zh_female_vv_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "pcm",  # 推荐默认值：局域网稳定环境下首音更快、播放更顺
            "stream": True,  # 推荐默认值：边合成边播放，首音延迟更低
        }
    },
    # OpenClaw Configuration
    "openclaw": {
        "url": "ws://127.0.0.1:18789",  # OpenClaw WebSocket 地址
        "token": "your_openclaw_token",  # OpenClaw 认证令牌
        "listening_light": {
            "enabled": True,  # 每轮本地监听时亮灯，结束或中断时熄灯
            "effect_id": 9,  # OH2P 固件 noangle 监听灯效，已在 OpenAI 对话里验证
        },
        # 输入模式：
        #   - "local_asr": 现有链路，使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 实验链路，唤醒小爱后接管原生 ASR 结果给 OpenClaw
        "input_mode": "local_asr",
        # session_key 格式：agent:<agentId>:<rest>
        #   agentId: OpenClaw 中配置的 Agent ID（默认为 main）
        #   rest:    会话标识，可自由命名，用于区分不同来源/场景 （默认为 open-xiaoai-bridge)
        # 也可在运行时动态切换（下一条消息即刻生效，无需重连）：
        #   app.set_openclaw_session_key("agent:assistant:open-xiaoai-bridge")
        "session_key": "agent:main:open-xiaoai-bridge",
        "identity_path": "/app/openclaw/identity/device.json",  # 设备身份文件路径；容器部署时建议挂载持久化目录
        "tts_speed": 1.0,  # TTS 语速 (0.5-2.0)，仅豆包 TTS 生效，小爱原生 TTS 不支持调速
        "tts_speaker": "xiaoai",  # "xiaoai" = 小爱原生 TTS；填豆包音色 ID 则用豆包 TTS；不设置则使用 tts.doubao.default_speaker
        # 可按 agentId 单独覆盖音色，优先级高于 tts_speaker
        # agentId 来自 session_key，格式为：agent:<agentId>:<rest>
        # 示例：
        # "agent_tts_speakers": {
        #     "assistant": "zh_female_vv_uranus_bigtts",
        #     "xiaomei": "zh_female_shuangkuaisisi_moon_bigtts",
        #     "butler": "xiaoai",
        # },
        "agent_tts_speakers": {},
        "response_timeout": 120,  # 等待 OpenClaw agent 响应的超时时间（秒）
        # 退出连续对话的词（规则见 core/exit_words.py）：只说退出词（可带"好的""谢谢"或唤醒词）马上退出；
        # "关灯，拜拜"先回答再退出；"停止播放音乐""用英语怎么说再见"照常发给后端。设成 [] 关闭
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        # rule_prompt: 用于「自动播放」和「连续对话」场景
        #   - send_to_openclaw_and_play_reply() 会自动追加
        #   - OpenClawConversationController 会自动追加
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        # rule_prompt_for_skill: 用于「Agent 自主播报」场景（方式三）
        #   - send_to_openclaw() 会自动追加
        #   - 告诉 Agent 需要调用 xiaoai-tts skill 来播报，因为服务端不会自动播放
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字，调用 `xiaoai-tts` skill 播报出来。字数控制在300字以内"
    },
    # OpenAI-compatible Service Configuration
    # 可接入 Hermes Agent API Server、OpenAI、Ollama、LM Studio 等兼容 /v1/chat/completions 的服务
    "openai": {
        # Defaults; start-bridge.bat env OPENAI_* overrides these (api_key via .hermes_api_key)
        "base_url": "https://hermes.oci.sounfury.top/v1",
        "api_key": "",
        "model": "muse-home",
        "listening_light": {
            "enabled": True,  # 等你说 9 → 听到你在说 41 → 思考 2 → 播报 3，退出时熄灭（见 core/services/conversation_light.py）
            "effect_id": 9,  # OH2P 固件 noangle 监听灯效
            "hearing_effect_id": 41,  # 听到你在说：固件 wakeup.sh 的 asrstart 灯效
            "thinking_effect_id": 2,  # 固件 wakeup.sh 的 think 灯效
            "speaking_effect_id": 3,  # 播报中（可打断）：固件 wakeup.sh 的 speek 灯效
        },
        "failure_text": "我听到了，但缪斯服务暂时没有返回回答，请稍后再试",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 统一采用 agent:<agentId>:<rest> 格式，便于 after_wakeup 解析
        "session_key": "agent:default:open-xiaoai-bridge",
        # 可选：把 session_key 作为请求头发给服务端，用于服务端长期记忆作用域。
        # 默认设为 Hermes 的 "X-Hermes-Session-Key"；它只用于长期记忆作用域，
        # chat/completions 仍是无状态（历史仍由 messages 携带），不会重复。
        # 接标准 OpenAI/Ollama/LM Studio 时该头会被忽略（无害），如需彻底关闭可留空。
        "session_header": "X-Hermes-Session-Key",
        "system_prompt": "",
        "temperature": 0.7,
        "max_tokens": 512,
        "history_max_messages": 20,
        "response_timeout": 120,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字。字数控制在300字以内",
        "extra_body": {},
    },
    # QwenPaw Configuration
    # 需先启动 QwenPaw: qwenpaw app
    "qwenpaw": {
        "base_url": "http://127.0.0.1:8088",
        # agent_id 仅作为回退：当 session_key 不含 agentId 时才使用。
        # 正常情况下 agentId 直接从 session_key 的第二段解析，无需单独配置。
        "agent_id": "default",
        "user_id": "open-xiaoai-bridge",
        "listening_light": {
            "enabled": True,  # 每轮本地监听时亮灯，结束或中断时熄灯
            "effect_id": 9,  # OH2P 固件 noangle 监听灯效，已在 OpenAI 对话里验证
        },
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 采用 agent:<agentId>:<sessionId> 格式：
        #   - 第二段 agentId 作为 X-Agent-Id 请求头发给服务端
        #   - 第三段起为 sessionId，作为请求体 session_id 发给服务端
        # 例如 agent:default:open-xiaoai-bridge -> agent=default, session=open-xiaoai-bridge
        "session_key": "agent:default:open-xiaoai-bridge",
        # QwenPaw 当前推荐使用后台任务接口：
        # POST /api/console/chat/task -> GET /api/console/chat/task/{task_id}
        "send_path": "/api/console/chat/task",
        "task_status_path": "/api/console/chat/task/{task_id}",
        # 认证（可选）：非空时默认使用 Authorization 认证头
        "auth_token": "",
        "response_timeout": 120,
        "poll_interval": 0.5,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["拜拜", "再见", "退出", "退下", "停止", "没事了", "不用了", "结束对话", "bye", "goodbye"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送给 QwenPaw 的，他看不到你回复的文字。字数控制在300字以内",
    },
    # 小爱管家（"你好缪斯"）唤醒词，需设置 MUSE_ENABLE=1
    # 命中后由 before_wakeup 路由到 OpenAI 兼容服务（Hermes）连续对话，对话参数见上面的 "openai"
    "muse": {
        # 规范唤醒词：始终参与匹配
        "wake_word": "你好缪斯",
        # ASR 文本前缀匹配的完整唤醒词集合（环境变量 MUSE_WAKE_WORDS 优先，用 , ， 、 分隔）
        # 匹配规则：
        #   - 忽略大小写、全半角、空格和中英文标点："hi muse" 也匹配 "Hi Muse" / "himuse" / "Hi，Muse"
        #   - 只在句首匹配；句首的标点和 leading_fillers 里的语气词可以跳过
        #   - 最长优先（"你好缪斯" 先于 "缪斯"）；英文唤醒词要求词边界（"museum" 不算 "muse"）
        # 单独的"缪斯" / "muse"只在句首生效；妙思 / 木斯 / 慕斯 本身是常见词，只接受带问候语的写法
        "wake_words": [
            # 正写 + 英文
            "你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi缪斯", "hey缪斯", "hello缪斯",
            "hi muse", "hey muse", "hello muse", "你好muse", "嗨muse", "哈喽muse", "嘿muse",
            "缪斯", "muse",
            # 同音字（miù sī），也可以单独使用
            "你好缪思", "嗨缪思", "哈喽缪思", "嘿缪思", "缪思",
            "你好缪丝", "嗨缪丝", "哈喽缪丝", "嘿缪丝", "缪丝",
            "你好谬斯", "嗨谬斯", "哈喽谬斯", "嘿谬斯", "谬斯",
            "你好谬思", "嗨谬思", "哈喽谬思", "嘿谬思", "谬思",
            # 近音字，只接受带问候语的写法
            "你好妙思", "嗨妙思", "哈喽妙思", "嘿妙思",
            "你好妙斯", "嗨妙斯", "哈喽妙斯", "嘿妙斯",
            "你好木斯", "嗨木斯", "哈喽木斯", "嘿木斯",
            "你好慕斯", "嗨慕斯", "哈喽慕斯", "嘿慕斯",
        ],
        # 合并进本地 KWS（常驻监听）的子集（环境变量 MUSE_KWS_WAKE_WORDS 优先；[] = 不额外加入）
        #   - 只放"问候语 + 名字"：单独的"缪斯" / "muse" 只有 2 个音节，常驻监听误唤醒风险高，只走 ASR 前缀
        #   - 同音/近音字只走 ASR：KWS 按汉字建模，每多一种写法就多一条常驻误唤醒路径
        #   - 这些词同时也会参与 ASR 前缀匹配；编码失败（模型词表没有的字）的会被跳过并告警
        "kws_wake_words": ["你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi muse", "hey muse", "hello muse"],
        "wake_word_aliases": [],  # 追加到 wake_words 的额外写法（兼容旧配置）
        # 唤醒词前可以跳过的语气词（只在句首，可重复）；设为 [] 关闭
        # 不含"那个"：它也是指示代词（"那个缪斯是谁"），去掉会让单独的"缪斯"更容易误触发
        "leading_fillers": ["嗯", "呃", "额", "啊", "喂", "诶", "欸", "哎", "um", "uh", "hmm", "emm"],
        # "prefix"（默认）：唤醒词必须在句首
        # "contains"：任意位置包含唤醒词即可——会让单独的"缪斯"/"muse"严重误触发，不建议开启
        "match_mode": "prefix",
        # 小爱原生识别出缪斯文本时如何拦截原生回复：
        #   "abort"  — speaker.abort_xiaoai()（重启小爱服务，约 1-2 秒）
        #   "dialog" — 复用 bridge 的 dialog 拦截（不重启，但原生回复晚到时可能打断开场提示）
        #   "none"   — 不拦截
        "native_suppress": "abort",
    },
}

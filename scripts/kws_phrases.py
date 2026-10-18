"""唤醒词验收用的语料（只用标准库，样本生成脚本和验收脚本共用）。

kind:
    wake      唤醒词（单说，或后面接着一句请求）：应该唤醒，expect 是期望命中的唤醒词
    negative  发音相近 / 日常说话：不应该唤醒
    name      句子中间提到"缪斯"：单独的"缪斯"进 KWS 时会被唤醒，单独统计，用来评估这个代价
    tv        长段电视 / 新闻风格的话：用来估算误唤醒率（次 / 小时）
    exit      对话中说的话：识别后按 core/exit_words.py 判断，expect 是期望的结果
              （now = 马上退出，after_answer = 回答完退出，none = 不退出）
"""

# 中文语音（edge-tts）；英文唤醒词另外加两个英文语音
ZH_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-TW-HsiaoChenNeural",
)
EN_VOICES = ("en-US-JennyNeural", "en-US-GuyNeural")
# 语速：正常、偏快、偏慢
RATES = ("+0%", "+20%", "-15%")

WAKE = [
    # (文本, 期望命中的唤醒词)
    ("你好缪斯", "你好缪斯"),
    ("嗨缪斯", "嗨缪斯"),
    ("哈喽缪斯", "哈喽缪斯"),
    ("嘿缪斯", "嘿缪斯"),
    ("缪斯", "缪斯"),
    ("hi muse", "hi muse"),
    ("hey muse", "hey muse"),
    ("hello muse", "hello muse"),
    # 唤醒词后面一口气接请求
    ("你好缪斯，开灯", "你好缪斯"),
    ("你好缪斯，明天天气怎么样", "你好缪斯"),
    ("嗨缪斯，把空调调到二十六度", "嗨缪斯"),
    ("嘿缪斯，放首歌", "嘿缪斯"),
    ("缪斯，关灯", "缪斯"),
    ("缪斯，现在几点了", "缪斯"),
    ("哈喽缪斯，今天有什么安排", "哈喽缪斯"),
    ("hey muse, turn on the light", "hey muse"),
    # 前面带语气词
    ("嗯，你好缪斯", "你好缪斯"),
    ("喂，缪斯", "缪斯"),
]
ENGLISH_WAKE = {"hi muse", "hey muse", "hello muse", "hey muse, turn on the light"}

NEGATIVE = [
    # 发音相近
    "慕斯蛋糕很好吃",
    "我老家在佳木斯",
    "博物馆几点开门",
    "他文思泉涌妙思不断",
    "你好，木头人",
    "你好小爱",
    "你好，请问现在几点",
    "嗨，好久不见",
    "哈喽，大家好",
    "嘿，你在干嘛",
    "谬论就是错误的说法",
    "牛肉面多少钱一碗",
    "秒杀活动马上开始",
    "我们去看音乐剧吧",
    "music is my life",
    "the museum is closed today",
    "amuse yourself",
    "hello world",
    "hey Siri",
    "你好小智",
    # 日常
    "今天晚上吃什么",
    "把门关一下",
    "妈妈我回来了",
    "这个周末要不要去爬山",
]

NAME_IN_SENTENCE = [
    "缪斯是希腊神话里的女神",
    "她是我的缪斯",
    "我们乐队叫缪斯",
    "这首歌是献给缪斯的",
]

TV = [
    "各位观众晚上好，欢迎收看今天的新闻联播。今天的主要内容有：国家统计局发布最新数据，前三季度国民经济运行总体平稳，"
    "就业形势保持稳定，居民消费价格温和上涨。气象部门提醒，受冷空气影响，北方大部地区将出现大风降温天气，请注意防寒保暖。",
    "接下来是体育新闻。昨晚进行的联赛中，主队凭借下半场的两粒进球，以二比一逆转战胜客队，继续领跑积分榜。"
    "赛后主教练表示，球队在困难的时候没有放弃，年轻球员的表现非常出色，接下来还要继续保持专注。",
    "你听我说，这件事情不是你想的那样。我那天确实去了那里，但是我只是去拿一份文件，根本没有见到他。"
    "你为什么就是不相信我呢？我们认识这么多年了，难道这点信任都没有吗？好吧，既然你这么想，那我们就把话说清楚。",
    "今天给大家介绍一道家常菜，番茄炒蛋。首先准备三个鸡蛋和两个番茄，鸡蛋打散加一点点盐，番茄切成小块。"
    "锅里倒油，油热后倒入蛋液，炒到凝固盛出来备用。然后炒番茄，炒出汁以后把鸡蛋倒回去，加一点糖，翻炒均匀就可以出锅了。",
    "好的，欢迎回到我们的节目。刚才那位嘉宾讲得非常精彩，现场的掌声也说明了一切。下面我们有请下一位选手，"
    "她今天带来的是一首非常经典的老歌，让我们用热烈的掌声欢迎她！音乐，起！",
    "近日，多地博物馆推出夜间开放活动，市民可以在晚上参观展览、参加讲座和手工体验。一位游客说，晚上来博物馆感觉很特别，"
    "人不多，可以慢慢看。工作人员介绍，夜场活动需要提前在网上预约，每天限流两千人。",
]


EXIT = [
    ("拜拜", "now"),
    ("再见", "now"),
    ("退出", "now"),
    ("停止", "now"),
    ("没事了", "now"),
    ("好的，谢谢，再见", "now"),
    ("你好缪斯，拜拜", "now"),
    ("拜拜，十五加十二等于多少", "now"),
    ("关灯，拜拜", "after_answer"),
    ("不用了，帮我把灯关掉", "after_answer"),
    ("用英语怎么说再见", "none"),
    ("再见用英语怎么说", "none"),
    ("停止播放音乐", "none"),
    ("帮我把客厅的灯关掉", "none"),
]


def phrases() -> list[dict]:
    """全部语料条目：{id, text, kind, expect, voices}。"""
    items = []
    for i, (text, expect) in enumerate(WAKE):
        voices = EN_VOICES + ZH_VOICES[:3] if text in ENGLISH_WAKE else ZH_VOICES
        items.append({"id": f"wake{i:02d}", "text": text, "kind": "wake", "expect": expect, "voices": list(voices)})
    for i, text in enumerate(NEGATIVE):
        english = text.isascii()
        voices = EN_VOICES if english else ZH_VOICES[:4]
        items.append({"id": f"neg{i:02d}", "text": text, "kind": "negative", "expect": None, "voices": list(voices)})
    for i, text in enumerate(NAME_IN_SENTENCE):
        items.append({"id": f"name{i:02d}", "text": text, "kind": "name", "expect": None, "voices": list(ZH_VOICES[:4])})
    for i, (text, expect) in enumerate(EXIT):
        items.append({"id": f"exit{i:02d}", "text": text, "kind": "exit", "expect": expect, "voices": list(ZH_VOICES[:4])})
    for i, text in enumerate(TV):
        items.append({"id": f"tv{i:02d}", "text": text, "kind": "tv", "expect": None, "voices": list(ZH_VOICES[:3])})
    return items

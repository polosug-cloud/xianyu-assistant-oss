"""闲鱼助手 - 全局配置（环境变量驱动，轻量部署适配）"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("XY_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.environ.get("XY_LOG_DIR", BASE_DIR / "logs"))
BROWSERS_DIR = Path(os.environ.get("XY_BROWSERS_DIR", BASE_DIR / ".browsers"))

for d in (DATA_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "xianyu.db"
SECRET_KEY_FILE = DATA_DIR / "secret.key"          # 本地加密密钥（自动生成，勿删）
SESSION_STATE_FILE = DATA_DIR / "storage_state.json"

APP_VERSION = "1.0.0"                               # 助手版本（托盘"关于"与状态接口展示）
APP_GITHUB = "https://github.com/polosug-cloud/xianyu-assistant-oss"   # 开源地址

WEB_USER = os.environ.get("XY_WEB_USER", "admin")
WEB_PASS = os.environ.get("XY_WEB_PASS", "")       # 空则随机生成并打印一次
HOST = os.environ.get("XY_HOST", "127.0.0.1")
PORT = int(os.environ.get("XY_PORT", "8080"))
# 账号身份标识（托盘启动实例时注入）：用于多实例/多副本环境下识别"该实例属于哪个账号"。
# 纯 ASCII，避免中文路径经由接口传输时的编码差异导致识别失败。
ACCOUNT_ID = os.environ.get("XY_ACCOUNT_ID", "")

# 提示音文件（sys=系统提示音·自检异常时响；msg=消息提示音·收到买家消息时响）。
# 默认读取本仓库 sounds/ 目录下的同名文件；可用环境变量 XY_SOUND_SYS / XY_SOUND_MSG 覆盖。
# 仓库不附带音频文件，请自行放入（缺失时仅静音，不影响其它功能）。
SOUND_SYS_FILE = os.environ.get("XY_SOUND_SYS", str(BASE_DIR / "sounds" / "Spring.ogg"))
SOUND_MSG_FILE = os.environ.get("XY_SOUND_MSG", str(BASE_DIR / "sounds" / "Bubble.ogg"))

# 内置图片资源目录（运行期还原）
DONATION_DIR = BASE_DIR / "assets" / "donation"
DONATION_PART_COUNT = 5

# 自动化参数（风控节流）
REPLY_MIN_INTERVAL_SEC = float(os.environ.get("XY_REPLY_INTERVAL", "4"))   # 回复最小间隔
DELIVER_MIN_INTERVAL_SEC = float(os.environ.get("XY_DELIVER_INTERVAL", "10"))  # 发货最小间隔
JITTER_RATIO = float(os.environ.get("XY_JITTER", "0.3"))                   # 随机抖动比例
MAX_ACTIONS_PER_HOUR = int(os.environ.get("XY_ACTION_HOURLY_CAP", "200"))  # 每小时动作上限
MESSAGE_POLL_SEC = float(os.environ.get("XY_POLL_SEC", "1"))               # 主循环间隔：越小 WS 回调泵出越快（默认 1s）
QR_REFRESH_SEC = int(os.environ.get("XY_QR_REFRESH", "15"))
# 扫码登录窗口最长等待秒数（默认 30 分钟；期间登录框被收起会自动重新唤起，不再"来不及扫码"）
LOGIN_WAIT_SEC = int(os.environ.get("XY_LOGIN_WAIT", "1800"))

# 规则回复引擎
RULE_COOLDOWN_SEC = int(os.environ.get("XY_RULE_COOLDOWN", "10"))          # 同一会话规则冷却

# AI 回复（二期，OpenAI 兼容）
AI_ENABLED = os.environ.get("XY_AI_ENABLED", "0") == "1"
AI_BASE_URL = os.environ.get("XY_AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.environ.get("XY_AI_API_KEY", "")
AI_MODEL = os.environ.get("XY_AI_MODEL", "deepseek-chat")
AI_MAX_INPUT_TOKENS = int(os.environ.get("XY_AI_MAX_INPUT", "2000"))
AI_SYSTEM_PROMPT = os.environ.get(
    "XY_AI_SYSTEM_PROMPT",
    "你是闲鱼卖家的自动客服，请用简短、礼貌、专业的中文回答买家问题。"
    "不确定的信息不要编造，涉及价格/库存以给定商品信息为准。"
)
